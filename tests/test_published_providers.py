"""The providers of published prices (EPEX Spot, GE-Spot, Octopus Energy, Nord Pool): the user's own prices of today and tomorrow, no forecast.

The rows of the fixtures are built from the shapes the sources document (their code); they are synthetic, no running installation of
the integrations was checked. Where the real integration has a datetime object in an attribute (GE-Spot, Octopus, Nord Pool) the
fixture has one too."""
from __future__ import annotations

import datetime
import pathlib
import zoneinfo

import pytest
import yaml
from homeassistant.core import HomeAssistant

from custom_components.cable_juice_planner.const import build_config, default_options
from custom_components.cable_juice_planner.prices import (
    EpexSpotPriceProvider,
    GeSpotPriceProvider,
    NordpoolPriceProvider,
    OctopusPriceProvider,
    create_price_provider,
    detect_price_scale,
    price_currency_errors,
    price_sensor_errors,
    provider_errors,
    providers_from_options,
)
from custom_components.cable_juice_planner.const import PRICE_PROVIDER_PRIORITY
from custom_components.cable_juice_planner.prices.base import price_unit_error
from custom_components.cable_juice_planner.prices.published_rows import epex_rows, gespot_raw_is_spot, gespot_rows, octopus_rows

from custom_components.cable_juice_planner.management.dashboard_build import TEMPLATE_DIR
from .conftest import providers_input
from .test_energypriceforecast import _balanced
from .test_price_country import _defaults, _offered, _to_price_provider

CHART_DIR = TEMPLATE_DIR / "prices"

COPENHAGEN = zoneinfo.ZoneInfo("Europe/Copenhagen")
LONDON = zoneinfo.ZoneInfo("Europe/London")
UTC = datetime.timezone.utc
TODAY = datetime.date(2026, 10, 30)
TOMORROW = TODAY + datetime.timedelta(days=1)
YESTERDAY = TODAY - datetime.timedelta(days=1)
DAY_3 = TODAY + datetime.timedelta(days=2)
NOW = datetime.datetime.combine(TODAY, datetime.time(10, 30))
TOTAL, MARKET = "sensor.epex_spot_data_total_price", "sensor.epex_spot_data_market_price"
GESPOT = "sensor.gespot_current_price_se3"
OCT_TODAY, OCT_TOMORROW = "event.octopus_energy_electricity_1_2_current_day_rates", "event.octopus_energy_electricity_1_2_next_day_rates"
OCT_EXPORT_TODAY, OCT_EXPORT_TOMORROW = "event.octopus_energy_electricity_1_2_export_current_day_rates", "event.octopus_energy_electricity_1_2_export_next_day_rates"
NORD, NORD_SELL = "sensor.nordpool_kwh_dk1_dkk_3_10_025", "sensor.nordpool_kwh_dk1_dkk_3_10_0"


class _State:
    def __init__(self, state: str, attributes: dict | None = None) -> None:
        self.state, self.attributes = state, attributes or {}


def _at(day: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(hour, minute))


def _starts(days, step: int = 60, zone=COPENHAGEN):
    """The aware starts of the periods of local days (in the zone), stepping in UTC like the sources."""
    for day in days:
        start = datetime.datetime.combine(day, datetime.time(0), tzinfo=zone).astimezone(UTC)
        end = datetime.datetime.combine(day + datetime.timedelta(days=1), datetime.time(0), tzinfo=zone)
        while start < end:
            yield start.astimezone(zone)
            start += datetime.timedelta(minutes=step)


def _price(day: datetime.date, hour: int) -> float:
    return 0.30 + (day - TODAY).days * 0.1 + hour * 0.01


def _epex(days, market: bool) -> list[dict]:
    return [
        {"start_time": s.isoformat(), "end_time": (s + datetime.timedelta(hours=1)).isoformat(), "price_per_kwh": _price(s.date(), s.hour) - (0.2 if market else 0.0)}
        for s in _starts(days)
    ]


def _gespot(days, step: int = 15) -> list[dict]:
    return [{"time": s, "value": _price(s.date(), s.hour), "raw_value": _price(s.date(), s.hour) - 0.2} for s in _starts(days, step)]


def _octopus(days, export: bool = False) -> list[dict]:
    return [
        {"start": s.astimezone(UTC), "end": (s + datetime.timedelta(minutes=30)).astimezone(UTC), "value_inc_vat": 0.04 + s.hour / 1000 if export else _price(s.date(), s.hour), "is_capped": False}
        for s in _starts(days, 30, LONDON)
    ]


def _nord(days, sell: bool) -> list[dict]:
    return [{"start": s, "end": s + datetime.timedelta(hours=1), "value": _price(s.date(), s.hour) - (0.2 if sell else 0.0)} for s in _starts(days)]


def _config(**options):
    return build_config({**default_options(), "sell_fee": 0.03, **options})


def _provider(cls, attributes: dict, tz=COPENHAGEN, **options):
    provider = cls(_config(**options))
    provider.shared_data = {"attributes": attributes, "values": {}, "version": 1}
    provider.now, provider.tz = NOW, tz
    provider.update_prices()
    return provider


EUR = {"unit_of_measurement": "€/kWh"}


def _epex_provider(**extra):
    attributes = {TOTAL: {**EUR, "data": _epex((YESTERDAY, TODAY, TOMORROW), False)}, MARKET: {**EUR, "data": _epex((YESTERDAY, TODAY, TOMORROW), True)}}
    return _provider(EpexSpotPriceProvider, attributes, epexspot_buy_entity_id=TOTAL, epexspot_sell_entity_id=MARKET, **extra)


# --- the parsers ------------------------------------------------------------------------------------------------------------------
def _local(value):
    from custom_components.cable_juice_planner.prices.base import price_timestamp

    return price_timestamp(value, COPENHAGEN)


def test_epex_rows_are_the_data_rows_and_a_bad_row_is_skipped() -> None:
    rows = epex_rows({"data": _epex((TODAY,), False) + [{"start_time": "x", "price_per_kwh": 1.0}, {"start_time": _epex((TODAY,), False)[0]["start_time"], "price_per_kwh": None}, "bad"]}, _local)
    assert len(rows) == 24 and rows[0] == (_at(TODAY, 0), pytest.approx(0.30)) and epex_rows({}, _local) == [] and epex_rows({"data": "no list"}, _local) == []


def test_ge_spot_tomorrow_is_only_read_when_it_is_valid_and_the_field_is_chosen() -> None:
    attributes = {"today_interval_prices": _gespot((TODAY,)), "tomorrow_interval_prices": _gespot((TOMORROW,)), "data_source": "nordpool"}
    assert {start.date() for start, _ in gespot_rows(attributes, _local, "value")} == {TODAY}  # no tomorrow_valid: not used
    assert {start.date() for start, _ in gespot_rows({**attributes, "tomorrow_valid": False}, _local, "value")} == {TODAY}
    assert {start.date() for start, _ in gespot_rows({**attributes, "tomorrow_valid": "yes"}, _local, "value")} == {TODAY}  # only a true
    both = gespot_rows({**attributes, "tomorrow_valid": True}, _local, "raw_value")
    assert {start.date() for start, _ in both} == {TODAY, TOMORROW} and len(both) == 192
    assert both[0][1] == pytest.approx(_price(TODAY, 0) - 0.2)


@pytest.mark.parametrize(("source", "spot"), [("nordpool", True), ("entsoe", True), ("energi_data_service", True), ("stromligning", False), ("Strømligning", False), ("amber", False), ("", False), (None, False)])
def test_ge_spot_raw_value_is_only_a_market_price_from_a_known_source_that_has_one(source, spot) -> None:
    assert gespot_raw_is_spot({"data_source": source}) is spot


def test_octopus_rows_are_pounds_and_rates_that_are_pence_make_the_list_unusable() -> None:
    assert len(octopus_rows({"rates": _octopus((TODAY,))}, _local)) == 48
    pence = _octopus((TODAY,))
    pence[5]["value_inc_vat"] = 24.5  # 24.5 pence, not pounds
    assert octopus_rows({"rates": pence}, _local) == []
    assert octopus_rows({}, _local) == []


# --- EPEX Spot --------------------------------------------------------------------------------------------------------------------
def test_epex_spot_buys_at_the_total_price_sells_at_the_market_price_less_the_fee_and_yesterday_is_no_price() -> None:
    provider = _epex_provider()
    assert set(provider.buy_real_prices) == {_at(day, hour) for day in (TODAY, TOMORROW) for hour in range(24)}  # not yesterday
    assert provider.buy_real_prices[_at(TODAY, 8)] == pytest.approx(_price(TODAY, 8))
    assert provider.sell_real_prices[_at(TODAY, 8)] == pytest.approx(_price(TODAY, 8) - 0.2 - 0.03, abs=0.0051)
    assert provider.buy_forecast_prices == {} and provider.sell_forecast_prices == {} and EpexSpotPriceProvider.has_forecast is False
    assert provider.get_buy_price_entity_id() == TOTAL and provider.get_sell_price_entity_id() == MARKET


def test_epex_spot_without_a_sell_sensor_has_no_sell_price_and_never_the_buy_price() -> None:
    provider = _provider(EpexSpotPriceProvider, {TOTAL: {**EUR, "data": _epex((TODAY,), False)}}, epexspot_buy_entity_id=TOTAL)
    assert provider.buy_real_prices and provider.sell_real_prices == {}


def test_a_sensor_that_is_not_loaded_is_an_error_and_keeps_the_old_prices() -> None:
    provider = _epex_provider()
    old = dict(provider.buy_real_prices)
    provider.shared_data = {"attributes": {TOTAL: {**EUR, "data": []}}, "values": {}, "version": 2}  # the sell sensor is gone
    with pytest.raises(ValueError, match="not loaded"):
        provider.update_prices()
    assert provider.buy_real_prices == old


@pytest.mark.parametrize("unit", ["EUR/MWh", "EUR/Wh", "ct/kWh", "c/kWh"])
def test_a_price_that_is_per_mwh_or_wh_or_in_a_subunit_gives_no_prices_for_every_provider(unit) -> None:
    epex = _provider(EpexSpotPriceProvider, {TOTAL: {"unit_of_measurement": unit, "data": _epex((TODAY,), False)}, MARKET: {**EUR, "data": _epex((TODAY,), True)}}, epexspot_buy_entity_id=TOTAL, epexspot_sell_entity_id=MARKET)
    assert epex.buy_real_prices == {}
    gespot = _provider(GeSpotPriceProvider, {GESPOT: {"unit_of_measurement": unit, "today_interval_prices": _gespot((TODAY,)), "data_source": "nordpool"}}, gespot_buy_entity_id=GESPOT)
    assert gespot.buy_real_prices == {} and gespot.sell_real_prices == {}
    nord = _provider(NordpoolPriceProvider, {NORD: {"unit_of_measurement": unit, "raw_today": _nord((TODAY,), False)}, NORD_SELL: {"unit_of_measurement": unit, "raw_today": _nord((TODAY,), True)}}, nordpool_buy_entity_id=NORD, nordpool_sell_entity_id=NORD_SELL)
    assert nord.buy_real_prices == {} and nord.sell_real_prices == {}  # a price per MWh is refused, not divided by 1000


# --- GE-Spot ----------------------------------------------------------------------------------------------------------------------
def _gespot_provider(source: str = "nordpool", valid: bool = True, **extra):
    attributes = {GESPOT: {"unit_of_measurement": "SEK/kWh", "today_interval_prices": _gespot((TODAY,)), "tomorrow_interval_prices": _gespot((TOMORROW,)), "tomorrow_valid": valid, "data_source": source}}
    return _provider(GeSpotPriceProvider, attributes, gespot_buy_entity_id=GESPOT, **extra)


def test_ge_spot_buys_at_value_sells_at_raw_value_less_the_fee_and_takes_tomorrow_only_when_it_is_valid() -> None:
    provider = _gespot_provider()
    assert len(provider.buy_real_prices) == 192 and provider.buy_real_prices[_at(TODAY, 8, 15)] == pytest.approx(_price(TODAY, 8))
    assert provider.sell_real_prices[_at(TOMORROW, 8)] == pytest.approx(_price(TOMORROW, 8) - 0.2 - 0.03, abs=0.0051)
    assert {start.date() for start in _gespot_provider(valid=False).buy_real_prices} == {TODAY}
    assert provider.buy_forecast_prices == {} and provider.get_sell_price_entity_id() == ""  # no sensor with the sell price to record


@pytest.mark.parametrize("source", ["stromligning", "amber"])
def test_ge_spot_raw_value_from_a_source_with_fees_and_taxes_is_no_sell_price_but_the_buy_price_stays(source) -> None:
    provider = _gespot_provider(source)
    assert provider.buy_real_prices and provider.sell_real_prices == {}
    switched = _gespot_provider("nordpool")
    switched.shared_data = {"attributes": {GESPOT: {**switched.shared_data["attributes"][GESPOT], "data_source": source}}, "values": {}, "version": 3}
    switched.update_prices()  # the integration fell back to another source during the day
    assert switched.sell_real_prices == {} and switched.buy_real_prices


# --- Octopus Energy ---------------------------------------------------------------------------------------------------------------
def _octopus_provider(export: bool = False, **extra):
    attributes = {OCT_TODAY: {"rates": _octopus((TODAY,))}, OCT_TOMORROW: {"rates": _octopus((TOMORROW,))}}
    options = {"octopus_buy_entity_id": OCT_TODAY, "octopus_buy_next_day_entity_id": OCT_TOMORROW}
    if export:
        attributes |= {OCT_EXPORT_TODAY: {"rates": _octopus((TODAY,), True)}, OCT_EXPORT_TOMORROW: {"rates": _octopus((TOMORROW,), True)}}
        options |= {"octopus_sell_entity_id": OCT_EXPORT_TODAY, "octopus_sell_next_day_entity_id": OCT_EXPORT_TOMORROW}
    return _provider(OctopusPriceProvider, attributes, LONDON, **options, **extra)  # Home Assistant of a British home is in London time


def test_octopus_buys_at_the_rates_of_today_and_tomorrow_in_pounds() -> None:
    provider = _octopus_provider()
    assert len(provider.buy_real_prices) == 96 and provider.buy_real_prices[_at(TODAY, 8, 30)] == pytest.approx(_price(TODAY, 8))
    # the UTC rows land on the clock of Home Assistant's time zone (London), half-hourly
    assert min(provider.buy_real_prices) == _at(TODAY, 0) and provider.buy_forecast_prices == {}
    assert max(provider.buy_real_prices) == _at(TOMORROW, 23, 30)
    assert OctopusPriceProvider.records_history is False and OctopusPriceProvider.countries == frozenset({"GB"})


def test_octopus_without_an_export_tariff_sells_for_nothing_and_with_one_for_its_rate() -> None:
    nothing = _octopus_provider()
    assert set(nothing.sell_real_prices) == set(nothing.buy_real_prices)
    assert set(nothing.sell_real_prices.values()) == {-0.03}  # 0 less the sell fee: never the buy price
    exported = _octopus_provider(export=True)
    assert exported.sell_real_prices[_at(TODAY, 8)] == pytest.approx(0.04 + 8 / 1000 - 0.03, abs=0.0051)
    assert max(exported.sell_real_prices.values()) < max(exported.buy_real_prices.values())


def test_octopus_rates_in_pence_give_no_prices_and_the_current_day_entity_is_required() -> None:
    pence = _octopus((TODAY,))
    pence[0]["value_inc_vat"] = 25.0
    provider = _provider(OctopusPriceProvider, {OCT_TODAY: {"rates": pence}, OCT_TOMORROW: {"rates": _octopus((TOMORROW,))}}, LONDON, octopus_buy_entity_id=OCT_TODAY, octopus_buy_next_day_entity_id=OCT_TOMORROW)
    assert {start.date() for start in provider.buy_real_prices} == {TOMORROW}  # today's list is unusable, tomorrow's is fine
    provider = OctopusPriceProvider(_config(octopus_buy_entity_id=OCT_TODAY, octopus_buy_next_day_entity_id=OCT_TOMORROW))
    provider.shared_data = {"attributes": {OCT_TOMORROW: {"rates": []}}, "values": {}, "version": 1}
    provider.now, provider.tz = NOW, LONDON
    with pytest.raises(ValueError, match="not loaded"):
        provider.update_prices()


def test_octopus_tomorrow_may_be_empty_until_it_is_published() -> None:
    provider = _provider(OctopusPriceProvider, {OCT_TODAY: {"rates": _octopus((TODAY,))}, OCT_TOMORROW: {"rates": []}}, LONDON, octopus_buy_entity_id=OCT_TODAY, octopus_buy_next_day_entity_id=OCT_TOMORROW)
    assert {start.date() for start in provider.buy_real_prices} == {TODAY}
    provider = _provider(OctopusPriceProvider, {OCT_TODAY: {"rates": _octopus((TODAY,))}}, LONDON, octopus_buy_entity_id=OCT_TODAY, octopus_buy_next_day_entity_id=OCT_TOMORROW)
    assert provider.buy_real_prices  # the next-day entity is not there (yet): the day of today stands


def test_octopus_has_no_recorded_history_to_read_and_its_rates_are_pounds_for_the_scale_and_the_currency() -> None:
    combined = create_price_provider(_config(octopus_buy_entity_id=OCT_TODAY, octopus_buy_next_day_entity_id=OCT_TOMORROW))
    assert combined.buy_price_entity_ids() == [] and combined.get_buy_price_entity_id() == OCT_TODAY
    states = {OCT_TODAY: _State("2026-10-30T10:00:00+00:00"), "sensor.eds": _State("1.0", {"unit_of_measurement": "DKK/kWh"})}  # an event has no unit
    assert detect_price_scale({"octopus_buy_entity_id": OCT_TODAY}, states.get) == 0.115
    mixed = {"octopus_buy_entity_id": OCT_TODAY, "energidataservice_buy_entity_id": "sensor.eds"}
    assert price_currency_errors(mixed, ["energidataservice_buy_entity_id"], states.get) == {"energidataservice_buy_entity_id": "price_currency_mismatch"}


# --- Nord Pool --------------------------------------------------------------------------------------------------------------------
def _nord_provider(**extra):
    attributes = {NORD: {"unit_of_measurement": "DKK/kWh", "raw_today": _nord((TODAY,), False), "raw_tomorrow": _nord((TOMORROW,), False), "raw_forecast": _nord((DAY_3,), False)},
                  NORD_SELL: {"unit_of_measurement": "DKK/kWh", "raw_today": _nord((TODAY,), True), "raw_tomorrow": _nord((TOMORROW,), True)}}
    return _provider(NordpoolPriceProvider, attributes, nordpool_buy_entity_id=NORD, nordpool_sell_entity_id=NORD_SELL, **extra)


def test_nord_pool_reads_its_rows_buys_at_the_price_with_vat_sells_at_the_sensor_without_it_and_has_no_forecast() -> None:
    provider = _nord_provider()
    assert set(provider.buy_real_prices) == {_at(day, hour) for day in (TODAY, TOMORROW) for hour in range(24)}
    assert provider.buy_real_prices[_at(TODAY, 8)] == pytest.approx(_price(TODAY, 8))
    assert provider.sell_real_prices[_at(TODAY, 8)] == pytest.approx(_price(TODAY, 8) - 0.2 - 0.03, abs=0.0051)
    assert provider.buy_forecast_prices == {} and provider.sell_forecast_prices == {}  # a raw_forecast attribute is not read
    assert NordpoolPriceProvider.has_forecast is False and provider.get_sell_price_entity_id() == NORD_SELL


def test_nord_pool_without_a_sell_sensor_has_no_sell_price_and_never_the_buy_price() -> None:
    provider = _provider(NordpoolPriceProvider, {NORD: {"unit_of_measurement": "DKK/kWh", "raw_today": _nord((TODAY,), False)}}, nordpool_buy_entity_id=NORD)
    assert provider.buy_real_prices and provider.sell_real_prices == {}


# --- registration, priority, forecast and country -----------------------------------------------------------------------------------
def test_the_providers_are_registered_and_stand_before_the_estimators() -> None:
    keys = {"epexspot_buy_entity_id": TOTAL, "gespot_buy_entity_id": GESPOT, "octopus_buy_entity_id": OCT_TODAY, "nordpool_buy_entity_id": NORD}
    assert set(providers_from_options(keys)) == {"octopus", "nordpool", "epexspot", "gespot"}
    assert PRICE_PROVIDER_PRIORITY.index("energidataservice") < PRICE_PROVIDER_PRIORITY.index("octopus")  # the Danish tariffs first
    for estimator in ("energypriceforecast", "spotforecast", "elprognos", "wattcast"):
        assert all(PRICE_PROVIDER_PRIORITY.index(name) < PRICE_PROVIDER_PRIORITY.index(estimator) for name in ("octopus", "nordpool", "epexspot", "gespot"))
    assert [type(provider) for provider in create_price_provider(_config(**keys)).providers] == [OctopusPriceProvider, NordpoolPriceProvider, EpexSpotPriceProvider, GeSpotPriceProvider]


def test_the_days_after_tomorrow_come_from_the_provider_with_a_forecast_and_today_from_the_users_own_prices() -> None:
    spot = "sensor.spot_forecast_de_lu_price"
    rows = [{"time": s.isoformat(), "price": 0.5, "source": "forecast", "p90": 0.6, "p10": 0.4, "h": 2} for s in _starts((DAY_3,))]
    rows += [{"time": s.isoformat(), "price": 0.5, "source": "market"} for s in _starts((TODAY, TOMORROW))]
    combined = create_price_provider(_config(nordpool_buy_entity_id=NORD, nordpool_sell_entity_id=NORD_SELL, spotforecast_buy_entity_id=spot, spot_vat=19.0, spot_surcharge=0.2))
    combined.set_now(NOW, COPENHAGEN)
    attributes = {
        NORD: {"unit_of_measurement": "DKK/kWh", "raw_today": _nord((TODAY,), False), "raw_tomorrow": _nord((TOMORROW,), False)},
        NORD_SELL: {"unit_of_measurement": "DKK/kWh", "raw_today": _nord((TODAY,), True), "raw_tomorrow": _nord((TOMORROW,), True)},
        spot: {"unit_of_measurement": "DKK/kWh", "prices": rows},
    }
    combined.set_snapshot({"attributes": attributes, "values": {}})
    assert combined.get_buy_prices()[_at(TODAY, 8)] == pytest.approx(_price(TODAY, 8))  # Nord Pool wins over the estimate (0.5 x 1.19 + 0.2)
    assert not combined.is_forecast(_at(TODAY, 8)) and not combined.is_forecast(_at(TOMORROW, 8))
    assert combined.is_forecast(_at(DAY_3, 8)) and combined.get_buy_prices()[_at(DAY_3, 8)] == pytest.approx(0.5 * 1.19 + 0.2 + 2 / 30, abs=0.01)


def test_none_of_them_has_a_forecast_so_each_alone_is_refused_and_with_one_that_has_a_forecast_is_accepted() -> None:
    for name, country in (("epexspot", "DE"), ("gespot", "SE"), ("octopus", "GB"), ("nordpool", "DK")):
        assert provider_errors(country, [name]) == {"base": "price_provider_no_forecast"}, name
    assert provider_errors("GB", ["octopus", "energypriceforecast"]) == {}
    assert provider_errors("SE", ["gespot", "elprognos"]) == {} and provider_errors("DE", ["epexspot", "spotforecast"]) == {}
    assert provider_errors("DK", ["nordpool", "energidataservice"]) == {}  # Energi Data Service has a forecast
    assert provider_errors("DK", ["octopus", "energidataservice"]) == {"base": "price_provider_country"}
    assert provider_errors("GB", ["nordpool", "energypriceforecast"]) == {"base": "price_provider_country"}
    assert provider_errors("PL", ["epexspot", "energypriceforecast"]) == {} and provider_errors("AU", ["gespot", "energypriceforecast"]) == {}  # any country


# --- the price page ---------------------------------------------------------------------------------------------------------------------
def test_a_sensor_without_rows_is_not_the_price_sensor_and_one_that_is_not_loaded_is_not_judged() -> None:
    core_nordpool = _State("0.52", {"unit_of_measurement": "DKK/kWh"})  # Home Assistant's own Nord Pool integration: no rows on its sensors
    real = _State("0.52", {"unit_of_measurement": "DKK/kWh", "raw_today": []})
    assert price_sensor_errors({"nordpool_buy_entity_id": NORD}, {NORD: core_nordpool}.get) == {"nordpool_buy_entity_id": "price_sensor_no_rows"}
    assert price_sensor_errors({"nordpool_buy_entity_id": NORD}, {NORD: real}.get) == {}
    assert price_sensor_errors({"nordpool_buy_entity_id": NORD}, {NORD: _State("unavailable", {})}.get) == {}
    assert price_sensor_errors({"nordpool_buy_entity_id": NORD}, {}.get) == {}
    assert price_sensor_errors({"epexspot_sell_entity_id": MARKET}, {MARKET: _State("0.1", EUR)}.get) == {"epexspot_sell_entity_id": "price_sensor_no_rows"}
    assert price_sensor_errors({"gespot_buy_entity_id": GESPOT}, {GESPOT: _State("1", {"unit_of_measurement": "SEK/kWh"})}.get) == {"gespot_buy_entity_id": "price_sensor_no_rows"}
    # an event entity that has not fired yet is "unknown": not judged; one with rates is fine, one with a state and no rates is not
    assert price_sensor_errors({"octopus_buy_entity_id": OCT_TODAY}, {OCT_TODAY: _State("unknown", {})}.get) == {}
    assert price_sensor_errors({"octopus_buy_entity_id": OCT_TODAY}, {OCT_TODAY: _State("2026-10-30T10:00:00+00:00", {"rates": []})}.get) == {}
    assert price_sensor_errors({"octopus_buy_entity_id": OCT_TODAY}, {OCT_TODAY: _State("2026-10-30T10:00:00+00:00", {})}.get) == {"octopus_buy_entity_id": "price_sensor_no_rows"}


def test_an_epex_spot_sensor_of_a_source_that_is_not_the_exchange_price_is_refused() -> None:
    states = {MARKET: _State("0.1", {**EUR, "data": []}), TOTAL: _State("0.3", {**EUR, "data": []})}
    ids = {MARKET: "epex_spot Awattar de MarketPrice", TOTAL: "epex_spot Tibber de Total Price"}
    errors = price_sensor_errors({"epexspot_buy_entity_id": TOTAL, "epexspot_sell_entity_id": MARKET}, states.get, ids.get)
    assert errors == {"epexspot_buy_entity_id": "price_sensor_not_exchange"}  # Tibber's total is not the exchange price
    ids[TOTAL] = "epex_spot Energyforecast.de de Total Price"
    assert price_sensor_errors({"epexspot_buy_entity_id": TOTAL}, states.get, ids.get) == {"epexspot_buy_entity_id": "price_sensor_not_exchange"}  # a forecast
    ids[TOTAL] = "epex_spot ENTSO-E-Transparency DE-LU Total Price"
    assert price_sensor_errors({"epexspot_buy_entity_id": TOTAL}, states.get, ids.get) == {}
    assert price_sensor_errors({"epexspot_buy_entity_id": TOTAL}, states.get) == {}  # without the registry nothing is known about it


def test_the_unit_rules_of_the_new_keys() -> None:
    for key in ("epexspot_buy_entity_id", "gespot_buy_entity_id", "nordpool_buy_entity_id", "nordpool_sell_entity_id", "octopus_buy_entity_id"):
        assert price_unit_error(key, "EUR/MWh") is True and price_unit_error(key, "EUR/Wh") is True and price_unit_error(key, "ct/kWh") is True, key
        assert price_unit_error(key, "EUR/kWh") is False and price_unit_error(key, "€/kWh") is False and price_unit_error(key, None) is False, key
    assert price_unit_error("gespot_buy_entity_id", "öre/kWh") is True and price_unit_error("gespot_buy_entity_id", "cents/kWh") is True  # its cents display


# --- the config flow ------------------------------------------------------------------------------------------------------------------
async def test_octopus_with_a_forecast_provider_in_great_britain_asks_for_its_rates_and_not_for_a_vat_and_a_surcharge(hass: HomeAssistant) -> None:
    result = await _to_price_provider(hass, price_country="GB")
    flow = hass.config_entries.flow
    refused = await flow.async_configure(result["flow_id"], providers_input("octopus"))
    assert refused["errors"] == {"base": "price_provider_no_forecast"}
    result = await flow.async_configure(result["flow_id"], providers_input("octopus", "energypriceforecast"))
    assert result["step_id"] == "prices_energypriceforecast"
    result = await flow.async_configure(result["flow_id"], {"energypriceforecast_buy_entity_id": "sensor.epf", "energypriceforecast_sell_entity_id": "sensor.epf_sell"})
    assert result["step_id"] == "prices_octopus"
    fields = {str(key): key for key in result["data_schema"].schema}
    assert set(fields) == {"octopus_buy_entity_id", "octopus_buy_next_day_entity_id", "octopus_sell_entity_id", "octopus_sell_next_day_entity_id"}
    assert [key.schema for key in fields.values() if type(key).__name__ == "Required"] == ["octopus_buy_entity_id", "octopus_buy_next_day_entity_id"]  # export is optional


async def test_nord_pool_is_not_offered_in_great_britain_and_epex_spot_is_offered_anywhere(hass: HomeAssistant) -> None:
    from homeassistant.data_entry_flow import InvalidData

    result = await _to_price_provider(hass, price_country="GB")
    flow = hass.config_entries.flow
    assert "nordpool" not in _offered(result) and {"octopus", "epexspot"} <= set(_offered(result))
    with pytest.raises(InvalidData):  # the page only takes what it lists
        await flow.async_configure(result["flow_id"], providers_input("nordpool", "energypriceforecast"))
    result = await _to_price_provider(hass, price_country="PL")
    assert "epexspot" in _offered(result)
    result = await flow.async_configure(result["flow_id"], providers_input("epexspot", "energypriceforecast"))
    assert result["step_id"] == "prices_energypriceforecast"
    result = await flow.async_configure(result["flow_id"], {"energypriceforecast_buy_entity_id": "sensor.epf", "energypriceforecast_sell_entity_id": "sensor.epf_sell"})
    assert result["step_id"] == "prices_epexspot"
    assert {str(key) for key in result["data_schema"].schema} == {"epexspot_buy_entity_id", "epexspot_sell_entity_id"}


async def test_the_fee_page_of_a_provider_of_published_prices_has_no_vat_and_surcharge(hass: HomeAssistant) -> None:
    from homeassistant.data_entry_flow import InvalidData

    result = await _to_price_provider(hass, price_country="DK")
    flow = hass.config_entries.flow
    result = await flow.async_configure(result["flow_id"], providers_input("nordpool", "energidataservice"))
    assert result["step_id"] == "prices_energidataservice"
    result = await flow.async_configure(result["flow_id"], {"energidataservice_buy_entity_id": "sensor.eds"})
    assert result["step_id"] == "prices_nordpool"
    with pytest.raises(InvalidData):  # both sensors are required: the sell price is not the buy price
        await flow.async_configure(result["flow_id"], {"nordpool_buy_entity_id": NORD})
    result = await flow.async_configure(result["flow_id"], {"nordpool_buy_entity_id": NORD, "nordpool_sell_entity_id": NORD_SELL})
    assert result["step_id"] == "price_fees" and not {"spot_vat", "spot_surcharge"} & {str(key) for key in result["data_schema"].schema}
    assert "sell_fee" in _defaults(result)


PUBLISHED_CHARTS = ("stromligning", "energidataservice", "energypriceforecast", "spotforecast", "elprognos", "wattcast", "epexspot", "gespot", "octopus", "nordpool")  # every provider
PLAN_ENTITY = "sensor.cjp_current_charging_rule"


def _chart(provider: str) -> dict:
    return yaml.safe_load((CHART_DIR / f"{provider}.yaml").read_text(encoding="utf-8"))


def _strings(node) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [text for value in node.values() for text in _strings(value)]
    if isinstance(node, list):
        return [text for value in node for text in _strings(value)]
    return []


def _adapter(text: str) -> str:
    start = text.index("const adapt = ")
    return text[start: text.index("\n};", start) + 3]


@pytest.mark.parametrize("provider", PUBLISHED_CHARTS)
def test_the_chart_of_every_provider_shows_the_prices_of_the_plan_for_7_days_through_one_adapter(provider: str) -> None:
    """The chart is the Energy Price Forecast chart with its data source changed: every read of the prices goes through adapt(), which
    reads the prices the planner uses (attribute price_chart of the plan entity: one row per price period for 7 days, real and forecast in
    one list, the sell price already less the sell fee). So it shows what the plan is made on, whatever provider gave the hours, and
    not only the days this provider publishes. The text is pinned here; that the JavaScript draws them was checked by running it in a
    headless browser (ha-dev/tools/chart_check.py), which is not part of this suite."""
    chart = _chart(provider)
    users = [text for text in _strings(chart) if "adapt(" in text]
    assert len(users) >= 7 and len({_adapter(text) for text in users}) == 1
    adapter = _adapter(users[0])
    assert "`" not in adapter and "${" not in adapter  # config-template-card evaluates ${ anywhere in the card
    assert all(_balanced(text) for text in users)
    text = (CHART_DIR / f"{provider}.yaml").read_text(encoding="utf-8")
    assert provider == "energypriceforecast" or ("energy_price_forecast" not in text and "Energy Price Forecast" not in text)  # nothing of the template
    assert "@@spot_vat@@" not in text and "@@spot_surcharge@@" not in text  # the plan's prices have them already
    assert f"states['{PLAN_ENTITY}']?.attributes?.price_chart" in adapter
    # real hours are today's rows, the forecast hours the forecast rows (the forecast zone starts at the first of them), 7 days of both
    assert "(r.forecast ? rows.raw_forecast : rows.raw_today).push({ start: r.time, value: value })" in adapter
    assert "typeof value === 'number' && Number.isFinite(value)" in adapter  # a missing price (null) is not 0
    assert "setDate" not in adapter and "wrongUnit" not in adapter  # no window of its own, no unit: the planner's prices are in the currency per kWh
    assert "@@sell_fee_value@@" not in text and "sellFee" not in text  # the sell fee is off in the planner's sell price already
    # no forecast hours: no forecast zone (not a guess from tomorrow on)
    assert any("if (!(eds.raw_forecast || []).length) return [];" in text for text in _strings(chart))
    assert any(text.startswith('${vars.total_hours ? (') for text in _strings(chart))
    # the plan entity is on the time axis too
    assert any("plan.map(p => new Date(p.time).getTime())" in text for text in _strings(chart))
    # the card renders again only when an entity of its list changes (an empty list never does): it watches the plan entity
    cards = []

    def find(node):
        if isinstance(node, dict):
            if node.get("type") == "custom:config-template-card":
                cards.append(node)
            for value in node.values():
                find(value)
        elif isinstance(node, list):
            for value in node:
                find(value)

    find(chart)
    assert [card["entities"] for card in cards] == [[PLAN_ENTITY]]

CHART_NAMES = {
    "stromligning": ("sensor.stromligning_current_price_vat", "Strømligning"), "energidataservice": ("sensor.energi_data_service", "Energi Data Service"),
    "energypriceforecast": ("sensor.energy_price_forecast", "Energy Price Forecast"), "spotforecast": ("sensor.spot_forecast", "Spot Forecast"),
    "elprognos": ("sensor.elpris_prognos", "Elprisprognos"), "wattcast": ("sensor.wattcast", "Wattcast"), "epexspot": ("sensor.epex_spot_total_price", "EPEX Spot"),
    "gespot": ("sensor.gespot_current_price", "GE-Spot"), "octopus": ("event.octopus_current_day_rates", "Octopus Energy"), "nordpool": ("sensor.nordpool", "Nord Pool"),
}


def test_the_charts_of_all_providers_differ_only_in_the_entity_and_the_name() -> None:
    """One chart for every provider: the generated files are the same text but for the entity of the restart card and the provider's name."""
    texts = {}
    for provider, (entity, name) in CHART_NAMES.items():
        text = (CHART_DIR / f"{provider}.yaml").read_text(encoding="utf-8")
        assert entity in text and name in text, provider
        texts[provider] = text.replace(entity, "@ENTITY@").replace(name, "@NAME@")
    assert set(CHART_NAMES) == set(PUBLISHED_CHARTS)
    assert len(set(texts.values())) == 1, {provider: len(text) for provider, text in texts.items()}


def _strict_json(path):
    import collections
    import json

    def no_duplicates(pairs):
        counts = collections.Counter(key for key, _ in pairs)
        assert all(count == 1 for count in counts.values()), (path.name, [key for key, count in counts.items() if count > 1])
        return dict(pairs)

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicates)


def test_no_text_file_has_a_key_twice_and_every_price_page_and_error_has_its_texts_in_every_language() -> None:
    """A key twice in a json file is read without a word (the last one wins): a text was once lost to another that way."""
    from custom_components.cable_juice_planner.const import FLOW_STEPS
    from custom_components.cable_juice_planner.prices.factory import PROVIDER_CLASSES

    directory = pathlib.Path(__file__).parents[1] / "custom_components" / "cable_juice_planner"
    files = [*sorted((directory / "translations").glob("*.json")), directory / "strings.json"]
    assert len(files) >= 6
    error_keys = ("price_sensor_no_rows", "price_sensor_not_exchange", "price_sensor_same_sensor", "price_sensor_wrong_role", "price_sensor_no_published", "price_sensor_forecast_only", "price_unit_subunit", "price_currency_mismatch")
    for path in files:
        texts = _strict_json(path)
        for section in ("config", "options"):
            assert all(texts[section]["error"].get(key) for key in error_keys), (path.name, section)
            for name in PROVIDER_CLASSES:
                step = f"prices_{name}"
                if step in FLOW_STEPS:
                    assert set(texts[section]["step"][step]["data"]) == set(FLOW_STEPS[step]) == set(texts[section]["step"][step]["data_description"]), (path.name, section, step)
                    assert texts[section]["step"][step]["title"] and texts[section]["step"][step]["description"], (path.name, section, step)
        options = texts["selector"]["price_providers"]["options"]
        assert all(options.get(name) for name in PROVIDER_CLASSES), (path.name, set(PROVIDER_CLASSES) - set(options))
