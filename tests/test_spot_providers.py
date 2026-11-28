"""The spot forecast providers (Spot Forecast, Elprisprognos, Wattcast): wholesale prices made into buy and sell prices.

The rows of the fixtures are built from the shapes the sources document (their code and README); they are synthetic, no running
installation of the integrations was checked."""
from __future__ import annotations

import datetime
import zoneinfo

import pytest
import yaml
from homeassistant.core import HomeAssistant

from custom_components.cable_juice_planner.const import build_config, default_options
from custom_components.cable_juice_planner.prices import (
    ElprognosPriceProvider,
    SpotForecastPriceProvider,
    WattcastPriceProvider,
    create_price_provider,
    detect_price_scale,
    price_currency_errors,
    price_sensor_errors,
    providers_from_options,
)
from custom_components.cable_juice_planner.prices.base import price_unit_error
from custom_components.cable_juice_planner.prices.spot_rows import elprognos_rows, spot_forecast_rows, wattcast_rows

from .conftest import providers_input
from .test_price_country import _defaults, _offered, _to_price_provider
from .test_energypriceforecast import _balanced, _generators
from custom_components.cable_juice_planner.management.dashboard_build import TEMPLATE_DIR

CHART_DIR = TEMPLATE_DIR / "prices"

BERLIN = zoneinfo.ZoneInfo("Europe/Berlin")
TODAY = datetime.date(2026, 10, 30)
TOMORROW = TODAY + datetime.timedelta(days=1)
DAY_3 = TODAY + datetime.timedelta(days=2)
NOW = datetime.datetime.combine(TODAY, datetime.time(10, 30))
CYGNUSB, ELPROGNOS, WATTCAST, WATTCAST_FORECAST = "sensor.spot_forecast_de_lu_price", "sensor.elpris_prognos_se3", "sensor.wattcast_fi_price_hour", "sensor.wattcast_fi_forecast"


class _State:
    def __init__(self, state: str, attributes: dict | None = None) -> None:
        self.state, self.attributes = state, attributes or {}


def _at(day: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(hour, minute))


def _iso(moment: datetime.datetime) -> str:
    """A local time as the ISO timestamp with its offset the sources give."""
    return moment.replace(tzinfo=BERLIN).isoformat()


def _utc(moment: datetime.datetime) -> str:
    return moment.replace(tzinfo=BERLIN).astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _to_local(value):
    from custom_components.cable_juice_planner.prices.base import price_timestamp

    return price_timestamp(value, BERLIN)


def _config(**options):
    return build_config({**default_options(), "spot_vat": 19.0, "spot_surcharge": 0.2, **options})


def _provider(cls, attributes: dict, **options):
    provider = cls(_config(**options))
    provider.shared_data = {"attributes": attributes, "values": {}, "version": 1}
    provider.now, provider.tz = NOW, BERLIN
    provider.update_prices()
    return provider


def _cygnusb_rows() -> list[dict]:
    rows = [{"time": _iso(_at(day, hour)), "price": price, "source": "market"} for day, price in ((TODAY, 0.10), (TOMORROW, 0.20)) for hour in range(24)]
    rows += [{"time": _iso(_at(DAY_3, hour)), "price": 0.30, "source": "forecast", "p90": 0.4, "p10": 0.2, "h": 2} for hour in range(24)]
    return rows


def test_spot_forecast_rows_are_published_where_the_source_says_market() -> None:
    rows = spot_forecast_rows({"prices": _cygnusb_rows() + [{"time": "x", "price": 1.0, "source": "market"}, {"time": _iso(_at(DAY_3, 5)), "price": None, "source": "market"}, "bad"]}, _to_local)
    assert len(rows) == 72
    assert rows[0] == (_at(TODAY, 0), 0.10, True) and rows[-1] == (_at(DAY_3, 23), 0.30, False)
    assert spot_forecast_rows({}, _to_local) == [] and spot_forecast_rows({"prices": "no list"}, _to_local) == []


def test_elprognos_rows_use_the_spot_price_not_the_value_with_vat() -> None:
    attributes = {"forecast": [
        {"start": _iso(_at(TODAY, 11)), "value": 9.99, "spot": 0.5, "forecast": False},
        {"start": _iso(_at(DAY_3, 11)), "value": 9.99, "spot": 0.6, "forecast": True},
        {"start": _iso(_at(DAY_3, 12)), "value": 9.99, "spot": 0.7},  # no flag: not taken for a published price
    ]}
    assert elprognos_rows(attributes, _to_local) == [(_at(TODAY, 11), 0.5, True), (_at(DAY_3, 11), 0.6, False), (_at(DAY_3, 12), 0.7, False)]


def _wattcast_attributes(known_until: str | None):
    price = {
        "unit_of_measurement": "ct/kWh",
        "raw_today": [{"start": _iso(_at(TODAY, hour)), "end": _iso(_at(TODAY, hour + 1)) if hour < 23 else _iso(_at(TOMORROW, 0)), "value": 10.0} for hour in range(24)],
        "raw_tomorrow": [{"start": _iso(_at(TOMORROW, hour)), "value": 20.0} for hour in range(24)],
    }
    forecast = {"known_until": known_until, "forecast": [{"start": _utc(_at(day, hour)), "p50": 30.0, "p10": 20.0, "p90": 40.0, "k": 1} for day in (TOMORROW, DAY_3) for hour in range(24)]}
    return price, forecast


def test_wattcast_rows_are_published_before_known_until_and_in_euro() -> None:
    price, forecast = _wattcast_attributes(_utc(_at(DAY_3, 0)))
    rows = wattcast_rows(price, forecast, _to_local)
    published = [row for row in rows if row[2]]
    assert len(published) == 48 and published[0] == (_at(TODAY, 0), 0.10, True) and published[-1] == (_at(TOMORROW, 23), 0.20, True)
    assert [row for row in rows if not row[2]] == [(_at(DAY_3, hour), 0.30, False) for hour in range(24)]  # tomorrow's hours are the price sensor's, not twice
    # tomorrow is not published yet: its rows hold the forecast and are not real, a missing known_until publishes nothing
    price, forecast = _wattcast_attributes(_utc(_at(TOMORROW, 0)))
    assert {row[2] for row in wattcast_rows(price, forecast, _to_local) if row[0].date() == TOMORROW} == {False}
    price, forecast = _wattcast_attributes(None)
    assert not any(row[2] for row in wattcast_rows(price, forecast, _to_local))


def test_the_buy_price_is_the_spot_price_with_vat_and_the_surcharge_and_the_sell_price_is_the_spot_price_less_the_fee() -> None:
    provider = _provider(SpotForecastPriceProvider, {CYGNUSB: {"unit_of_measurement": "EUR/kWh", "prices": _cygnusb_rows()}}, spotforecast_buy_entity_id=CYGNUSB, sell_fee=0.03)
    assert provider.buy_real_prices[_at(TODAY, 8)] == pytest.approx(0.10 * 1.19 + 0.2)
    assert provider.sell_real_prices[_at(TODAY, 8)] == pytest.approx(0.07)  # no VAT, no surcharge, the fee off
    assert set(provider.buy_real_prices) == {_at(day, hour) for day in (TODAY, TOMORROW) for hour in range(24)}
    # two days ahead: 2/30 on top of a forecast (dearer to buy, a sale is worth less)
    assert provider.buy_forecast_prices[_at(DAY_3, 8)] == pytest.approx(0.30 * 1.19 + 0.2 + 2 / 30, abs=0.01)
    assert provider.sell_forecast_prices[_at(DAY_3, 8)] == pytest.approx(0.30 - 2 / 30 - 0.03, abs=0.01)
    assert provider.get_buy_price_entity_id() == CYGNUSB and provider.get_sell_price_entity_id() == ""


def test_a_forecast_never_enters_an_hour_with_a_published_price_and_today_without_one_is_not_planned() -> None:
    rows = _cygnusb_rows()
    rows += [{"time": _iso(_at(TODAY, 3, minute)), "price": 9.0, "source": "forecast"} for minute in (0, 15, 30, 45)]  # inside a published hour
    rows += [{"time": _iso(_at(TOMORROW, 5, 15)), "price": 9.0, "source": "forecast"}]
    provider = _provider(SpotForecastPriceProvider, {CYGNUSB: {"unit_of_measurement": "EUR/kWh", "prices": rows}}, spotforecast_buy_entity_id=CYGNUSB)
    assert len(provider.buy_forecast_prices) == 24 and all(start.date() == DAY_3 for start in provider.buy_forecast_prices)
    # a day without published prices (only the forecast of today) gives no price for it
    only_forecast = [{"time": _iso(_at(TODAY, hour)), "price": 0.3, "source": "forecast"} for hour in range(24)]
    provider = _provider(SpotForecastPriceProvider, {CYGNUSB: {"unit_of_measurement": "EUR/kWh", "prices": only_forecast}}, spotforecast_buy_entity_id=CYGNUSB)
    assert provider.buy_real_prices == {} and provider.buy_forecast_prices == {}


def test_without_a_vat_and_a_surcharge_there_are_no_prices() -> None:
    attributes = {CYGNUSB: {"unit_of_measurement": "EUR/kWh", "prices": _cygnusb_rows()}}
    for options in ({"spot_vat": None}, {"spot_surcharge": None}):
        provider = SpotForecastPriceProvider(build_config({**default_options(), "spotforecast_buy_entity_id": CYGNUSB, **{"spot_vat": 19.0, "spot_surcharge": 0.2, **options}}))
        provider.shared_data = {"attributes": attributes, "values": {}, "version": 1}
        provider.now, provider.tz = NOW, BERLIN
        with pytest.raises(ValueError, match="not set"):
            provider.update_prices()
        assert provider.buy_real_prices == {}  # never a buy price without them


def test_elprognos_makes_prices_from_its_spot_rows() -> None:
    rows = [{"start": _iso(_at(day, hour)), "value": 9.99, "spot": 0.5, "forecast": day == DAY_3} for day in (TODAY, TOMORROW, DAY_3) for hour in range(24)]
    provider = _provider(ElprognosPriceProvider, {ELPROGNOS: {"unit_of_measurement": "SEK/kWh", "forecast": rows}}, elprognos_buy_entity_id=ELPROGNOS, spot_vat=25.0, spot_surcharge=0.9)
    assert provider.buy_real_prices[_at(TODAY, 12)] == pytest.approx(0.5 * 1.25 + 0.9, abs=0.01)
    assert provider.buy_forecast_prices[_at(DAY_3, 12)] == pytest.approx(0.5 * 1.25 + 0.9 + 2 / 30, abs=0.01)
    assert detect_price_scale({"elprognos_buy_entity_id": ELPROGNOS}, {ELPROGNOS: _State("0.5", {"unit_of_measurement": "SEK/kWh"})}.get) == 1.4


def _wattcast_provider(known_until: str | None, unit: str = "ct/kWh"):
    price, forecast = _wattcast_attributes(known_until)
    price["unit_of_measurement"] = unit
    return _provider(WattcastPriceProvider, {WATTCAST: price, WATTCAST_FORECAST: forecast}, wattcast_buy_entity_id=WATTCAST, wattcast_forecast_entity_id=WATTCAST_FORECAST)


def test_wattcast_gives_euro_prices_from_ct_per_kwh_and_nothing_in_another_unit() -> None:
    provider = _wattcast_provider(_utc(_at(DAY_3, 0)))
    assert provider.buy_real_prices[_at(TODAY, 8)] == pytest.approx(0.10 * 1.19 + 0.2)  # 10 ct/kWh is 0.10 euro
    assert provider.buy_forecast_prices[_at(DAY_3, 8)] == pytest.approx(0.30 * 1.19 + 0.2 + 2 / 30, abs=0.01)
    for unit in ("EUR/kWh", "ct/MWh", "kr/kWh"):
        assert _wattcast_provider(_utc(_at(DAY_3, 0)), unit).buy_real_prices == {}, unit  # the ct is the source's own unit, no other is read as it
    assert _wattcast_provider(None).buy_real_prices == {}  # nothing is known to be published


def test_wattcast_needs_its_forecast_sensor() -> None:
    price, _forecast = _wattcast_attributes(None)
    provider = WattcastPriceProvider(_config(wattcast_buy_entity_id=WATTCAST, wattcast_forecast_entity_id=WATTCAST_FORECAST))
    provider.shared_data = {"attributes": {WATTCAST: price}, "values": {}, "version": 1}
    provider.now, provider.tz = NOW, BERLIN
    with pytest.raises(ValueError, match="not loaded"):
        provider.update_prices()


def test_the_unit_rules_ct_per_kwh_is_only_wattcasts() -> None:
    assert price_unit_error("wattcast_buy_entity_id", "ct/kWh") is False and price_unit_error("wattcast_buy_entity_id", "EUR/kWh") is True
    assert price_unit_error("wattcast_buy_entity_id", None) is False  # a sensor that is down has no unit: not judged
    for key in ("spotforecast_buy_entity_id", "elprognos_buy_entity_id", "energypriceforecast_buy_entity_id", "energidataservice_buy_entity_id"):
        assert price_unit_error(key, "ct/kWh") is True, key
    states = {WATTCAST: _State("10", {"unit_of_measurement": "ct/kWh"}), CYGNUSB: _State("10", {"unit_of_measurement": "ct/kWh"})}
    assert price_sensor_errors({"wattcast_buy_entity_id": WATTCAST}, states.get) == {}
    assert price_sensor_errors({"spotforecast_buy_entity_id": CYGNUSB}, states.get) == {"spotforecast_buy_entity_id": "price_unit_subunit"}


def test_wattcast_prices_are_in_euro_for_the_scale_and_the_currency_check() -> None:
    states = {WATTCAST: _State("10", {"unit_of_measurement": "ct/kWh"}), ELPROGNOS: _State("0.5", {"unit_of_measurement": "SEK/kWh"})}
    assert detect_price_scale({"wattcast_buy_entity_id": WATTCAST}, states.get) == 0.134
    mixed = {"wattcast_buy_entity_id": WATTCAST, "elprognos_buy_entity_id": ELPROGNOS}
    assert price_currency_errors(mixed, ["elprognos_buy_entity_id"], states.get) == {"elprognos_buy_entity_id": "price_currency_mismatch"}


def test_the_recorded_state_of_a_wholesale_sensor_is_never_read_as_a_buy_price() -> None:
    """The state of the sensors is a wholesale price (or a mean of a forecast): the economy and the price history must not use it."""
    combined = create_price_provider(_config(spotforecast_buy_entity_id=CYGNUSB, elprognos_buy_entity_id=ELPROGNOS))
    assert [type(provider) for provider in combined.providers] == [SpotForecastPriceProvider, ElprognosPriceProvider]
    assert combined.buy_price_entity_ids() == [] and combined.sell_price_entity_ids() == []
    assert combined.combine_history({CYGNUSB: {1.0: 0.1}, ELPROGNOS: {1.0: 0.2}}) == {}
    assert combined.get_buy_price_entity_id() == f"{CYGNUSB}, {ELPROGNOS}"  # still named in a message
    assert providers_from_options({"wattcast_buy_entity_id": WATTCAST, "elprognos_buy_entity_id": ELPROGNOS}) == ["elprognos", "wattcast"]


def test_the_countries_of_the_spot_providers() -> None:
    assert SpotForecastPriceProvider.countries == frozenset({"DE", "LU", "NL", "NO"}) and "DK" not in SpotForecastPriceProvider.countries
    assert ElprognosPriceProvider.countries == frozenset({"SE"}) and WattcastPriceProvider.countries == frozenset({"EE", "FI", "LV", "LT"})


def _published_guard_states(rows_key: str, rows: list[dict], entity_id: str, unit: str = "EUR/kWh") -> dict:
    return {entity_id: _State("0.1", {"unit_of_measurement": unit, rows_key: rows})}


def test_a_spot_sensor_with_rows_but_none_published_is_refused_on_the_page() -> None:
    """Not the price sensor of the integration, or a shape that is not read: it would give no price for today."""
    unpublished = [{**row, "source": "forecast"} for row in _cygnusb_rows()]
    keys = {"spotforecast_buy_entity_id": CYGNUSB}
    refused = {"spotforecast_buy_entity_id": "price_sensor_no_published"}
    assert price_sensor_errors(keys, _published_guard_states("prices", unpublished, CYGNUSB).get) == refused
    assert price_sensor_errors(keys, _published_guard_states("prices", _cygnusb_rows(), CYGNUSB).get) == {}
    assert price_sensor_errors(keys, {CYGNUSB: _State("0.1", {"unit_of_measurement": "EUR/kWh"})}.get) == {}  # no rows yet: not judged
    assert price_sensor_errors(keys, {CYGNUSB: _State("unavailable", {"unit_of_measurement": "EUR/kWh", "prices": unpublished})}.get) == {}
    elprognos = [{"start": _iso(_at(TODAY, hour)), "value": 1.0, "spot": 0.5} for hour in range(24)]  # no forecast flag: none published
    assert price_sensor_errors({"elprognos_buy_entity_id": ELPROGNOS}, _published_guard_states("forecast", elprognos, ELPROGNOS, "SEK/kWh").get) == {"elprognos_buy_entity_id": "price_sensor_no_published"}
    elprognos = [{**row, "forecast": False} for row in elprognos]
    assert price_sensor_errors({"elprognos_buy_entity_id": ELPROGNOS}, _published_guard_states("forecast", elprognos, ELPROGNOS, "SEK/kWh").get) == {}


def test_a_wattcast_sensor_without_a_known_until_is_refused_on_the_page_and_a_missing_forecast_sensor_is_not_judged() -> None:
    values = {"wattcast_buy_entity_id": WATTCAST, "wattcast_forecast_entity_id": WATTCAST_FORECAST}
    price, forecast = _wattcast_attributes(None)
    states = {WATTCAST: _State("10", price), WATTCAST_FORECAST: _State("1", forecast)}
    assert price_sensor_errors(values, states.get) == {"wattcast_buy_entity_id": "price_sensor_no_published"}
    price, forecast = _wattcast_attributes(_utc(_at(DAY_3, 0)))
    states = {WATTCAST: _State("10", price), WATTCAST_FORECAST: _State("1", forecast)}
    assert price_sensor_errors(values, states.get) == {}
    assert price_sensor_errors(values, {WATTCAST: _State("10", price)}.get) == {}  # the forecast sensor is not there (yet): not judged
    assert price_sensor_errors({"wattcast_buy_entity_id": WATTCAST}, states.get) == {}  # and not chosen yet


def test_a_price_per_mwh_or_wh_is_wrong_for_the_spot_sensors_and_gives_no_prices() -> None:
    for key in ("spotforecast_buy_entity_id", "elprognos_buy_entity_id"):
        for unit in ("EUR/MWh", "SEK/MWh", "EUR/Wh"):
            assert price_unit_error(key, unit) is True, (key, unit)
        assert price_unit_error(key, "EUR/kWh") is False
    for unit in ("EUR/MWh", "EUR/Wh"):
        provider = _provider(SpotForecastPriceProvider, {CYGNUSB: {"unit_of_measurement": unit, "prices": _cygnusb_rows()}}, spotforecast_buy_entity_id=CYGNUSB)
        assert provider.buy_real_prices == {} and provider.sell_real_prices == {}, unit  # never an 85 per kWh from 85 per MWh
    assert price_sensor_errors({"spotforecast_buy_entity_id": CYGNUSB}, {CYGNUSB: _State("85", {"unit_of_measurement": "EUR/MWh"})}.get) == {"spotforecast_buy_entity_id": "price_unit_subunit"}


def test_a_vat_and_a_surcharge_of_zero_still_give_prices() -> None:
    """0 % VAT and a surcharge of 0 are values the user entered: only a missing value (None) means no prices."""
    provider = _provider(SpotForecastPriceProvider, {CYGNUSB: {"unit_of_measurement": "EUR/kWh", "prices": _cygnusb_rows()}}, spotforecast_buy_entity_id=CYGNUSB, spot_vat=0.0, spot_surcharge=0.0)
    assert provider.buy_real_prices[_at(TODAY, 8)] == pytest.approx(0.10)


def test_a_mixed_setup_names_only_the_providers_with_a_usable_history() -> None:
    combined = create_price_provider(_config(energypriceforecast_buy_entity_id="sensor.epf", spotforecast_buy_entity_id=CYGNUSB))
    assert [provider.source_name for provider in combined.providers] == ["energypriceforecast", "spotforecast"]
    assert combined.buy_price_entity_ids() == ["sensor.epf"]


def test_a_sensor_with_no_published_row_is_named_once_in_the_log(caplog) -> None:
    rows = [{**row, "source": "forecast"} for row in _cygnusb_rows()]
    provider = SpotForecastPriceProvider(_config(spotforecast_buy_entity_id=CYGNUSB))
    provider.shared_data = {"attributes": {CYGNUSB: {"unit_of_measurement": "EUR/kWh", "prices": rows}}, "values": {}, "version": 1}
    provider.now, provider.tz = NOW, BERLIN
    with caplog.at_level("WARNING"):
        provider.update_prices()
        provider.update_prices()
    assert [record.getMessage() for record in caplog.records if "none of them is published" in record.getMessage()] == [
        f"{CYGNUSB} has price rows but none of them is published: check that it is the price sensor of the integration"
    ]


def test_a_wattcast_forecast_sensor_in_another_unit_gives_no_prices_and_one_without_a_unit_is_read() -> None:
    """The forecast sensor says its unit (forecast_unit): its p50 is divided by 100 as ct/kWh, so another unit is wrong for it."""
    price, forecast = _wattcast_attributes(_utc(_at(DAY_3, 0)))
    attributes = {WATTCAST: price, WATTCAST_FORECAST: {**forecast, "forecast_unit": "EUR/MWh"}}
    provider = _provider(WattcastPriceProvider, attributes, wattcast_buy_entity_id=WATTCAST, wattcast_forecast_entity_id=WATTCAST_FORECAST)
    assert provider.buy_real_prices == {} and provider.buy_forecast_prices == {}
    for unit in ("ct/kWh", None):
        attributes[WATTCAST_FORECAST] = {**forecast, **({"forecast_unit": unit} if unit else {})}
        provider = _provider(WattcastPriceProvider, attributes, wattcast_buy_entity_id=WATTCAST, wattcast_forecast_entity_id=WATTCAST_FORECAST)
        assert provider.buy_real_prices and provider.buy_forecast_prices, unit


def test_published_quarter_hour_rows_give_a_price_per_quarter_and_no_forecast_in_their_hours() -> None:
    """Spot Forecast's market rows have been 15 minutes since October 2025, its forecast rows are hourly."""
    rows = [{"time": _iso(_at(day, hour, minute)), "price": 0.10 + minute / 1000, "source": "market"} for day in (TODAY, TOMORROW) for hour in range(24) for minute in (0, 15, 30, 45)]
    rows += [{"time": _iso(_at(TOMORROW, 5)), "price": 9.0, "source": "forecast"}]  # inside a published hour
    rows += [{"time": _iso(_at(DAY_3, hour)), "price": 0.30, "source": "forecast", "p90": 0.4, "p10": 0.2, "h": 2} for hour in range(24)]
    provider = _provider(SpotForecastPriceProvider, {CYGNUSB: {"unit_of_measurement": "EUR/kWh", "prices": rows}}, spotforecast_buy_entity_id=CYGNUSB)
    assert len(provider.buy_real_prices) == 192
    assert provider.buy_real_prices[_at(TODAY, 8, 30)] == pytest.approx(0.13 * 1.19 + 0.2)
    assert provider.buy_real_prices[_at(TODAY, 8, 0)] != provider.buy_real_prices[_at(TODAY, 8, 45)]
    assert len(provider.buy_forecast_prices) == 24 and all(start.date() == DAY_3 for start in provider.buy_forecast_prices)


async def test_the_fee_page_asks_for_the_vat_and_the_surcharge_without_a_default_and_only_for_these_providers(hass: HomeAssistant) -> None:
    from homeassistant.data_entry_flow import InvalidData

    result = await _to_price_provider(hass, price_country="SE")
    flow = hass.config_entries.flow
    result = await flow.async_configure(result["flow_id"], providers_input("elprognos"))
    assert result["step_id"] == "prices_elprognos"
    result = await flow.async_configure(result["flow_id"], {"elprognos_buy_entity_id": ELPROGNOS})
    assert result["step_id"] == "price_fees"
    assert set(_defaults(result)) == {"refund", "sell_fee"}  # no default for the VAT and the surcharge: they are not in the defaults
    with pytest.raises(InvalidData):
        await flow.async_configure(result["flow_id"], {"refund": 0.0, "sell_fee": 0.03})
    result = await flow.async_configure(result["flow_id"], {"refund": 0.0, "sell_fee": 0.03, "spot_vat": 25.0, "spot_surcharge": 0.9})
    assert result["step_id"] == "solar_forecast_source"


async def test_the_fee_page_has_no_vat_and_surcharge_for_the_other_providers(hass: HomeAssistant) -> None:
    result = await _to_price_provider(hass, price_country="DK")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], providers_input("energidataservice"))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"energidataservice_buy_entity_id": "sensor.eds"})
    assert result["step_id"] == "price_fees" and "spot_vat" not in {str(key) for key in result["data_schema"].schema}


async def test_wattcast_is_offered_in_finland_with_both_sensors_and_not_in_denmark(hass: HomeAssistant) -> None:
    result = await _to_price_provider(hass, price_country="DK")
    assert "wattcast" not in _offered(result) and "elprognos" not in _offered(result)
    result = await _to_price_provider(hass, price_country="FI")
    assert "wattcast" in _offered(result)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], providers_input("wattcast"))
    assert result["step_id"] == "prices_wattcast"
    assert {str(key) for key in result["data_schema"].schema} == {"wattcast_buy_entity_id", "wattcast_forecast_entity_id"}


def test_the_dashboard_maps_only_the_buy_sensor_of_a_spot_chart_to_the_configured_one() -> None:
    """Only the buy sensor is the chart's own (the restart card): the chart itself draws the plan's prices (test_published_providers.py)."""
    from custom_components.cable_juice_planner.management.dashboard_build import render_dashboard

    from .test_dashboard import _own
    from .test_engine import OPTIONS

    ids = {"sensor.wattcast_fi_price_hour", "sensor.wattcast_fi_forecast"}
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status", "sensor.price", *ids}
    base = {**OPTIONS, "language": "en", "energidataservice_buy_entity_id": "", "energidataservice_sell_entity_id": "", "sell_fee": 0.03}
    wattcast = {"wattcast_buy_entity_id": "sensor.wattcast_fi_price_hour", "wattcast_forecast_entity_id": "sensor.wattcast_fi_forecast", "spot_vat": 25.5, "spot_surcharge": 0.08}
    text = render_dashboard(build_config({**base, **wattcast}), "tesla", "entry1", existing, "EUR")
    assert "sensor.wattcast_fi_price_hour" in text and "'sensor.wattcast'" not in text and "@@" not in text
    assert "sensor.wattcast_fi_forecast" not in text  # Wattcast's forecast sensor is not the chart's: it draws the plan's prices


def test_a_wattcast_forecast_unit_that_is_wrong_is_named_once_in_the_log_and_forgotten_when_it_is_right() -> None:
    import logging

    price, forecast = _wattcast_attributes(_utc(_at(DAY_3, 0)))
    provider = WattcastPriceProvider(_config(wattcast_buy_entity_id=WATTCAST, wattcast_forecast_entity_id=WATTCAST_FORECAST))
    provider.shared_data = {"attributes": {WATTCAST: price, WATTCAST_FORECAST: forecast}, "values": {}, "version": 1}
    provider.now, provider.tz = NOW, BERLIN
    messages: list[str] = []

    class Catch(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    logger = logging.getLogger("custom_components.cable_juice_planner.prices.spotprices")
    handler = Catch(level=logging.WARNING)
    logger.addHandler(handler)
    try:
        forecast["forecast_unit"] = "EUR/MWh"
        provider.update_prices()
        provider.update_prices()
        assert [message for message in messages if "forecast in EUR/MWh" in message] == [f"{WATTCAST_FORECAST} gives its forecast in EUR/MWh, not in ct/kWh as Wattcast does: its prices are not used"]
        forecast["forecast_unit"] = "ct/kWh"
        provider.update_prices()
        assert provider.buy_real_prices
        forecast["forecast_unit"] = "EUR/MWh"
        provider.update_prices()
        assert len([message for message in messages if "forecast in EUR/MWh" in message]) == 2  # right again, wrong again: named again
    finally:
        logger.removeHandler(handler)
