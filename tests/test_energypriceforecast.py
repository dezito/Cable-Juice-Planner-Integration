"""The Energy Price Forecast provider: published prices and forecast rows {start, end, value}, UTC rows, sell price, the flow."""
from __future__ import annotations

import datetime
import zoneinfo

import pytest
import yaml

from custom_components.cable_juice_planner.management.dashboard_build import TEMPLATE_DIR
from custom_components.cable_juice_planner.const import PRICE_PROVIDER_KEYS, build_config, default_options
from custom_components.cable_juice_planner.prices import (
    EnergiDataServicePriceProvider,
    EnergyPriceForecastPriceProvider,
    create_price_provider,
    detect_price_scale,
    price_currency_errors,
    price_dashboard_entities,
    price_sensor_errors,
    providers_from_options,
)
from custom_components.cable_juice_planner.prices.currency import energypriceforecast_entities, is_forecast_only, is_price_subunit

COPENHAGEN = zoneinfo.ZoneInfo("Europe/Copenhagen")
UTC = datetime.timezone.utc
TODAY = datetime.date(2026, 10, 30)
TOMORROW = TODAY + datetime.timedelta(days=1)
DAY_3 = TODAY + datetime.timedelta(days=2)
NOW = datetime.datetime.combine(TODAY, datetime.time(10, 30))
BUY, SELL = "sensor.epf_retail", "sensor.epf_wholesale"


class _State:
    def __init__(self, state: str, attributes: dict | None = None) -> None:
        self.state = state
        self.attributes = attributes or {}


def _at(hour: int, minute: int = 0, day: datetime.date = TODAY) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(hour, minute))


def _utc(moment: datetime.datetime) -> str:
    """A local wall-clock time as the UTC timestamp the integration reports ("Z")."""
    return moment.replace(tzinfo=COPENHAGEN).astimezone(UTC).isoformat().replace("+00:00", "Z")


def _rows(day: datetime.date, price, step: int = 60) -> list[dict]:
    """Every period of a local day as UTC rows {start, end, value}."""
    rows = []
    moment = datetime.datetime.combine(day, datetime.time(0))
    while moment.date() == day:
        end = moment + datetime.timedelta(minutes=step)
        rows.append({"start": _utc(moment), "end": _utc(end), "value": price(moment)})
        moment = end
    return rows


def _config(**options):
    return build_config({**default_options(), "energypriceforecast_buy_entity_id": BUY, **options})


def _provider(attributes: dict, tz=COPENHAGEN, **options) -> EnergyPriceForecastPriceProvider:
    provider = EnergyPriceForecastPriceProvider(_config(**options))
    provider.shared_data = {"attributes": attributes, "values": {}, "version": 1}
    provider.now, provider.tz = NOW, tz
    provider.update_prices()
    return provider


def test_the_provider_is_registered_after_the_danish_ones() -> None:
    config = _config(energidataservice_buy_entity_id="sensor.eds", stromligning_buy_current_price="sensor.sl")
    combined = create_price_provider(config)
    assert [provider.source_name for provider in combined.providers] == ["stromligning", "energidataservice", "energypriceforecast"]
    assert providers_from_options({"energypriceforecast_buy_entity_id": BUY}) == ["energypriceforecast"]
    assert providers_from_options({}) == ["energidataservice"]  # nothing chosen yet: the flow still starts with Energi Data Service
    assert create_price_provider(_config()).buy_price_entity_ids() == [BUY]


def test_published_prices_are_real_and_the_forecast_gets_the_adder() -> None:
    attributes = {
        BUY: {
            "raw_today": _rows(TODAY, lambda m: 1.0 + m.hour),
            "raw_tomorrow": _rows(TOMORROW, lambda m: 10.0),
            "raw_forecast": _rows(DAY_3, lambda m: 20.0),
        }
    }
    provider = _provider(attributes)
    assert provider.buy_real_prices == {**{_at(hour): 1.0 + hour for hour in range(24)}, **{_at(hour, day=TOMORROW): 10.0 for hour in range(24)}}
    # two days ahead: 2 / 30 on top of the forecast (a forecast is dearer than a known price)
    assert provider.buy_forecast_prices[_at(8, day=DAY_3)] == pytest.approx(20.0 + 2 / 30)
    assert provider.get_buy_price_entity_id() == BUY


def test_a_forecast_row_never_replaces_a_published_price() -> None:
    attributes = {BUY: {"raw_today": _rows(TODAY, lambda m: 1.0), "raw_tomorrow": _rows(TOMORROW, lambda m: 2.0), "raw_forecast": _rows(TOMORROW, lambda m: 99.0)}}
    provider = _provider(attributes)
    assert provider.buy_forecast_prices == {}
    combined = create_price_provider(_config())
    combined.set_now(NOW, COPENHAGEN)
    combined.set_snapshot({"attributes": attributes, "values": {}})
    assert combined.get_buy_prices()[_at(8, day=TOMORROW)] == 2.0 and not combined.is_forecast(_at(8, day=TOMORROW))


def test_tomorrow_is_empty_until_it_is_published() -> None:
    attributes = {BUY: {"raw_today": _rows(TODAY, lambda m: 1.0), "raw_tomorrow": [], "raw_forecast": _rows(TOMORROW, lambda m: 5.0)}}
    provider = _provider(attributes)
    assert set(provider.buy_real_prices) == {_at(hour) for hour in range(24)}
    assert provider.buy_forecast_prices[_at(8, day=TOMORROW)] == pytest.approx(5.0 + 1 / 30)


def test_today_comes_only_from_published_prices_and_a_sensor_with_only_a_forecast_is_refused() -> None:
    """A market without published prices (Finland, Great Britain, ...): raw_today is empty and today's quarters from the
    present are in raw_forecast. They are not used (no price before the present), and the price page refuses the sensor:
    the own price source of Energy Price Forecast gives such a market published prices."""
    today = [row for row in _rows(TODAY, lambda m: 3.0, step=15) if row["start"] >= _utc(_at(10, 30))]
    attributes = {BUY: {"raw_today": [], "raw_tomorrow": [], "raw_forecast": today + _rows(TOMORROW, lambda m: 5.0, step=15)}}
    provider = _provider(attributes)
    assert provider.buy_real_prices == {} and min(provider.buy_forecast_prices) == _at(0, day=TOMORROW)
    retail = {"unit_of_measurement": "EUR/kWh", "retail_source": "formula", **attributes[BUY]}
    values = {"energypriceforecast_buy_entity_id": BUY}
    assert price_sensor_errors(values, {BUY: _State("0.3", retail)}.get) == {"energypriceforecast_buy_entity_id": "price_sensor_forecast_only"}
    published = {**retail, "raw_today": _rows(TODAY, lambda m: 1.0)}
    assert price_sensor_errors(values, {BUY: _State("0.3", published)}.get) == {}
    assert price_sensor_errors(values, {BUY: _State("0.3", {**retail, "raw_forecast": []})}.get) == {}  # nothing at all yet: not judged
    assert price_sensor_errors(values, {BUY: _State("unavailable", retail)}.get) == {}  # a sensor that is down is not judged
    after_midnight = {**retail, "raw_tomorrow": _rows(TODAY, lambda m: 1.0)}  # today's rows still in raw_tomorrow
    assert price_sensor_errors(values, {BUY: _State("0.3", after_midnight)}.get) == {}
    # the sell sensor (the wholesale series) needs published prices as well
    wholesale = {"unit_of_measurement": "EUR/kWh", **attributes[BUY]}
    both = {"energypriceforecast_buy_entity_id": BUY, "energypriceforecast_sell_entity_id": SELL}
    states = {BUY: _State("0.3", published), SELL: _State("0.1", wholesale)}
    assert price_sensor_errors(both, states.get) == {"energypriceforecast_sell_entity_id": "price_sensor_forecast_only"}
    # the more specific errors still win over a sensor with only a forecast
    same = {"energypriceforecast_buy_entity_id": BUY, "energypriceforecast_sell_entity_id": BUY}
    assert price_sensor_errors(same, {BUY: _State("0.3", retail)}.get) == {
        "energypriceforecast_buy_entity_id": "price_sensor_forecast_only",
        "energypriceforecast_sell_entity_id": "price_sensor_not_wholesale",
    }
    assert price_sensor_errors(values, {BUY: _State("0.1", wholesale)}.get) == {"energypriceforecast_buy_entity_id": "price_sensor_not_retail"}
    # the current buy price is the real one, else the forecast (never missing, which would book a charge at 0)
    combined = create_price_provider(_config())
    combined.set_now(NOW, COPENHAGEN)
    combined.set_snapshot({"attributes": {BUY: published}, "values": {}})
    assert combined.get_buy_price(_at(12)) == 1.0
    combined.set_snapshot({"attributes": {BUY: {**published, "raw_forecast": _rows(TOMORROW, lambda m: 5.0)}}, "values": {}})
    assert combined.get_buy_price(_at(8, day=TOMORROW)) == pytest.approx(5.0 + 1 / 30, abs=0.01)


def test_a_published_price_of_today_still_wins_over_a_forecast_row_of_today() -> None:
    attributes = {BUY: {"raw_today": _rows(TODAY, lambda m: 1.0), "raw_forecast": _rows(TODAY, lambda m: 99.0)}}
    provider = _provider(attributes)
    assert provider.buy_real_prices[_at(12)] == 1.0 and provider.buy_forecast_prices == {}


def test_a_forecast_quarter_never_enters_an_hour_with_a_published_price() -> None:
    """Published hours next to forecast quarters (two lists with different periods): the hour keeps its published price."""
    forecast = _rows(TODAY, lambda m: 99.0, step=15) + _rows(TOMORROW, lambda m: 99.0, step=15) + _rows(DAY_3, lambda m: 7.0, step=15)
    attributes = {BUY: {"raw_today": _rows(TODAY, lambda m: 1.0), "raw_tomorrow": _rows(TOMORROW, lambda m: 2.0), "raw_forecast": forecast}}
    provider = _provider(attributes)  # tomorrow's quarters :15, :30 and :45 are not published timestamps, but their hour is
    assert all(timestamp.date() == DAY_3 for timestamp in provider.buy_forecast_prices)
    assert len(provider.buy_forecast_prices) == 96


class _Stub:
    def __init__(self, name: str, real: dict, forecast: dict) -> None:
        self.source_name, self._real, self._forecast, self.sell_price_details = name, real, forecast, {}

    def get_buy_real_prices(self):
        return self._real

    def get_buy_forecast_prices(self):
        return self._forecast


def test_a_forecast_of_another_provider_never_enters_an_hour_with_a_real_price() -> None:
    """An hourly real price of one provider and forecast quarters of the next: the real price is the price of the hour."""
    combined = create_price_provider(_config())
    combined.providers = [
        _Stub("hourly", {_at(10): 1.0}, {}),
        _Stub("quarters", {}, {_at(10, minute): 99.0 for minute in (0, 15, 30, 45)} | {_at(11, 15): 5.0}),
    ]
    prices, real, sources, _details = combined._combine("buy")
    assert prices == {_at(10): 1.0, _at(11, 15): 5.0} and real == {_at(10): 1.0}
    assert sources[_at(11, 15)] == "quarters:forecast"


def test_a_missing_or_wrong_attribute_gives_no_prices_instead_of_an_error() -> None:
    attributes = {BUY: {"raw_today": None, "raw_tomorrow": "bad", "raw_forecast": [1, None, {"start": "x", "value": 1}, {"start": _utc(_at(9)), "value": "n/a"}]}}
    provider = _provider(attributes)
    assert provider.buy_real_prices == {} and provider.buy_forecast_prices == {}


def test_a_sensor_that_is_not_loaded_is_an_error_for_the_combined_provider_to_handle() -> None:
    provider = EnergyPriceForecastPriceProvider(_config())
    provider.shared_data = {"attributes": {}, "values": {}, "version": 1}
    with pytest.raises(ValueError):
        provider.update_prices()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), None, True, "1.0"])
def test_rows_without_a_finite_price_are_skipped(bad) -> None:
    rows = _rows(TODAY, lambda m: 1.0)
    rows[5]["value"] = bad
    provider = _provider({BUY: {"raw_today": rows}})
    assert _at(5) not in provider.buy_real_prices and len(provider.buy_real_prices) == 23


def test_utc_rows_land_on_the_local_hour_and_quarter() -> None:
    provider = _provider({BUY: {"raw_today": _rows(TODAY, lambda m: m.hour + m.minute / 100, step=15)}})
    assert len(provider.buy_real_prices) == 96
    assert provider.buy_real_prices[_at(0)] == 0.0 and provider.buy_real_prices[_at(17, 45)] == pytest.approx(17.45)
    # without a time zone the UTC hours are not shifted: the offset is just dropped
    provider = _provider({BUY: {"raw_today": _rows(TODAY, lambda m: m.hour)}}, tz=None)
    assert provider.buy_real_prices[_at(21)] == 22  # the row of 22:00 local time is 21:00 UTC (CET on 30 October)


def test_the_repeated_autumn_hour_is_one_key_and_the_first_row_wins() -> None:
    """25 October 2026: 02:00 CEST and 02:00 CET are the same wall-clock time."""
    day = datetime.date(2026, 10, 25)
    rows = [{"start": f"2026-10-24T{hour:02d}:00:00Z", "end": "", "value": float(hour)} for hour in range(22, 24)]
    rows += [{"start": f"2026-10-25T{hour:02d}:00:00Z", "end": "", "value": float(hour)} for hour in range(0, 23)]
    provider = EnergyPriceForecastPriceProvider(_config())
    provider.shared_data = {"attributes": {BUY: {"raw_today": rows}}, "values": {}, "version": 1}
    provider.now, provider.tz = datetime.datetime.combine(day, datetime.time(8)), COPENHAGEN
    provider.update_prices()
    prices = provider.buy_real_prices
    assert len(prices) == 24  # 25 hours of rows, 24 local hours
    assert prices[datetime.datetime.combine(day, datetime.time(2))] == 0.0  # 00:00 UTC = 02:00 CEST, the first of the two
    assert prices[datetime.datetime.combine(day, datetime.time(3))] == 2.0  # 02:00 UTC = 03:00 CET


def test_without_a_sell_sensor_the_sell_price_is_calculated_from_the_buy_sensor() -> None:
    provider = _provider({BUY: {"raw_today": _rows(TODAY, lambda m: 2.5), "raw_forecast": _rows(DAY_3, lambda m: 2.5)}}, sell_fee=0.03)
    assert provider.get_sell_price_entity_id() == BUY
    assert provider.sell_real_prices[_at(17)] == pytest.approx(2.47)
    # a sale at a forecast price is worth less: the adder is taken off (production_price -1 = no fixed price)
    assert provider.sell_forecast_prices[_at(17, day=DAY_3)] == pytest.approx(round(2.5 - 2 / 30 - 0.03, 2))


def test_a_sell_sensor_gives_the_sell_price_and_nothing_but_the_fee_is_taken_off() -> None:
    attributes = {
        BUY: {"raw_today": _rows(TODAY, lambda m: 3.0)},
        SELL: {"raw_today": _rows(TODAY, lambda m: 1.0)},
    }
    provider = _provider(attributes, energypriceforecast_sell_entity_id=SELL, sell_fee=0.05)
    assert provider.get_sell_price_entity_id() == SELL
    assert provider.buy_real_prices[_at(8)] == 3.0
    assert provider.sell_real_prices[_at(8)] == pytest.approx(0.95)  # no tariff of the buy price is subtracted from it
    details = provider.sell_price_details[_at(8)]
    assert details["tariff_sum"] == 0.0 and details["sell_transmissions_nettarif"] == 0.0 and details["sell_fee"] == 0.05


def test_the_prices_of_a_stale_sensor_are_what_the_sensor_shows() -> None:
    """data_status is not read: a stale sensor still has its rows, and when they are gone the combined provider falls
    back to the recorded prices as for any other provider."""
    attributes = {BUY: {"data_status": "stale", "raw_today": _rows(TODAY, lambda m: 1.0)}}
    assert len(_provider(attributes).buy_real_prices) == 24


def test_it_works_next_to_energi_data_service_with_the_priority_order() -> None:
    eds = {"raw_today": [{"hour": _at(hour).isoformat(), "price": 3.0} for hour in range(12)]}
    config = _config(energidataservice_buy_entity_id="sensor.eds")
    combined = create_price_provider(config)
    assert isinstance(combined.providers[0], EnergiDataServicePriceProvider)
    combined.set_now(NOW, COPENHAGEN)
    combined.set_snapshot({"attributes": {"sensor.eds": eds, BUY: {"raw_today": _rows(TODAY, lambda m: 1.0)}}, "values": {}})
    prices = combined.get_buy_prices()
    assert prices[_at(8)] == 3.0 and prices[_at(20)] == 1.0  # Energi Data Service first, Energy Price Forecast fills the rest
    assert combined.price_source(_at(8)) == "energidataservice:real" and combined.price_source(_at(20)) == "energypriceforecast:real"


def test_the_dashboard_maps_the_entities_of_its_chart() -> None:
    prices = {key: "" for keys in PRICE_PROVIDER_KEYS.values() for key in keys}
    prices.update({"energypriceforecast_buy_entity_id": BUY, "energypriceforecast_sell_entity_id": SELL})
    mapping = price_dashboard_entities(prices)
    assert mapping["sensor.energy_price_forecast"] == BUY and mapping["sensor.energy_price_forecast_sell"] == SELL
    assert mapping["sensor.energi_data_service"] == ""  # one chart: the first configured provider in priority order


@pytest.mark.parametrize(
    ("unit", "scale"), [("EUR/kWh", 0.134), ("GBP/kWh", 0.115), ("PLN/kWh", 0.57), ("€/kWh", 0.134), ("GBp/kWh", None), ("ct/kWh", None), ("EUR/MWh", 0.134)]
)
def test_the_price_scale_is_found_from_the_unit_of_the_sensor(unit, scale) -> None:
    states = {BUY: _State("0.2", {"unit_of_measurement": unit})}
    assert detect_price_scale({"energypriceforecast_buy_entity_id": BUY}, states.get) == scale


@pytest.mark.parametrize(
    ("unit", "subunit"),
    [
        ("ct/kWh", True), ("c/kWh", True), ("EURct/kWh", True), ("EUR cents/kWh", True), ("GBp/kWh", True), ("p/kWh", True), ("øre/kWh", True), ("¢/kWh", True),
        ("EUR/kWh", False), ("GBP/kWh", False), ("kr/kWh", False), ("DKK/kWh", False), ("EUR/MWh", False), ("ct/MWh", True), (None, False), ("", False),
    ],
)
def test_a_price_in_a_subunit_of_the_currency_is_recognised(unit, subunit) -> None:
    assert is_price_subunit(unit) is subunit


def test_the_price_pages_refuse_a_price_in_cents_or_pence_and_a_wholesale_buy_sensor() -> None:
    retail = {"unit_of_measurement": "EUR/kWh", "raw_today": [], "retail_source": "estimate"}
    states = {
        "sensor.retail": _State("0.2", retail),
        "sensor.cents": _State("20", {**retail, "unit_of_measurement": "ct/kWh"}),
        "sensor.wholesale": _State("0.1", {"unit_of_measurement": "EUR/kWh", "raw_today": []}),
        "sensor.down": _State("unavailable", {}),
    }
    ok = price_sensor_errors({"energypriceforecast_buy_entity_id": "sensor.retail", "energypriceforecast_sell_entity_id": "sensor.wholesale"}, states.get)
    assert ok == {}  # a wholesale sensor is what the sell sensor is
    assert price_sensor_errors({"energypriceforecast_buy_entity_id": "sensor.cents"}, states.get) == {"energypriceforecast_buy_entity_id": "price_unit_subunit"}
    assert price_sensor_errors({"energypriceforecast_buy_entity_id": "sensor.wholesale"}, states.get) == {"energypriceforecast_buy_entity_id": "price_sensor_not_retail"}
    assert price_sensor_errors({"energypriceforecast_sell_entity_id": "sensor.cents"}, states.get) == {"energypriceforecast_sell_entity_id": "price_unit_subunit"}
    # the retail price is more than a sale pays: not as the sell sensor, and not the same sensor for both
    assert price_sensor_errors({"energypriceforecast_sell_entity_id": "sensor.retail"}, states.get) == {"energypriceforecast_sell_entity_id": "price_sensor_not_wholesale"}
    same = {"energypriceforecast_buy_entity_id": "sensor.retail", "energypriceforecast_sell_entity_id": "sensor.retail"}
    assert price_sensor_errors(same, states.get) == {"energypriceforecast_sell_entity_id": "price_sensor_not_wholesale"}
    assert price_sensor_errors({"energypriceforecast_buy_entity_id": "sensor.wholesale", "energypriceforecast_sell_entity_id": "sensor.wholesale"}, states.get) == {
        "energypriceforecast_buy_entity_id": "price_sensor_not_retail", "energypriceforecast_sell_entity_id": "price_sensor_not_wholesale"}
    assert price_sensor_errors({"energidataservice_buy_entity_id": "sensor.cents"}, states.get) == {"energidataservice_buy_entity_id": "price_unit_subunit"}
    # not loaded, not found or not chosen: not judged
    assert price_sensor_errors({"energypriceforecast_buy_entity_id": "sensor.down"}, states.get) == {}
    assert price_sensor_errors({"energypriceforecast_buy_entity_id": "sensor.missing", "energypriceforecast_sell_entity_id": ""}, states.get) == {}


def test_price_sensors_in_two_currencies_are_refused_on_the_page_that_adds_the_second() -> None:
    """A backup provider is only a backup within one currency: its prices would be planned as if they were the first's."""
    states = {
        "sensor.eds": _State("1.2", {"unit_of_measurement": "DKK/kWh"}),
        BUY: _State("0.2", {"unit_of_measurement": "EUR/kWh"}),
        "sensor.epf_dkk": _State("1.2", {"unit_of_measurement": "kr/kWh"}),
        "sensor.nounit": _State("1.2", {}),
    }
    keys = ["energypriceforecast_buy_entity_id", "energypriceforecast_sell_entity_id"]
    options = {"energidataservice_buy_entity_id": "sensor.eds", "energypriceforecast_buy_entity_id": BUY}
    assert price_currency_errors(options, keys, states.get) == {"energypriceforecast_buy_entity_id": "price_currency_mismatch"}
    # the same currency in another spelling, a sensor without a unit and a page without a second provider: fine
    assert price_currency_errors({**options, "energypriceforecast_buy_entity_id": "sensor.epf_dkk"}, keys, states.get) == {}
    assert price_currency_errors({**options, "energypriceforecast_buy_entity_id": "sensor.nounit"}, keys, states.get) == {}
    assert price_currency_errors({"energypriceforecast_buy_entity_id": BUY}, keys, states.get) == {}
    # only the keys of the page are marked: the first provider's page was accepted before
    assert price_currency_errors(options, ["energidataservice_buy_entity_id"], states.get) == {"energidataservice_buy_entity_id": "price_currency_mismatch"}
    assert price_currency_errors(options, ["stromligning_buy_current_price"], states.get) == {}


def _balanced(js: str) -> bool:
    """Brackets of a piece of JavaScript pair up (strings, template literals and comments are skipped)."""
    backslash, newline = chr(92), chr(10)
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    i, quote = 0, ""
    while i < len(js):
        c = js[i]
        if quote:
            if c == backslash:
                i += 1
            elif c == quote:
                quote = ""
        elif js.startswith("//", i):
            end = js.find(newline, i)
            i = len(js) if end < 0 else end
        elif js.startswith("/*", i):
            end = js.find("*/", i)
            i = len(js) if end < 0 else end + 1
        elif c in ("\"", "'", "`"):
            quote = c
        elif c in "([{":
            stack.append(c)
        elif c in pairs:
            if not stack or stack.pop() != pairs[c]:
                return False
        i += 1
    return not stack and not quote


def _generators(node) -> list[str]:
    found = []
    if isinstance(node, dict):
        found += [node["data_generator"]] if isinstance(node.get("data_generator"), str) else []
        for value in node.values():
            found += _generators(value)
    elif isinstance(node, list):
        for value in node:
            found += _generators(value)
    return found


@pytest.mark.parametrize("provider", ["energidataservice", "stromligning", "energypriceforecast"])
def test_every_chart_generator_is_complete_javascript(provider: str) -> None:
    """The data generators of the price charts are JavaScript in a yaml string: a cut-off or unbalanced one breaks the whole chart."""
    chart = yaml.safe_load((TEMPLATE_DIR / "prices" / f"{provider}.yaml").read_text(encoding="utf-8"))
    generators = _generators(chart)
    assert len(generators) >= 5
    assert all(_balanced(js) for js in generators), provider
    if provider == "energypriceforecast":
        zone = next(js for js in generators if "forecast_zone" in js)
        # the forecast zone starts at the first forecast row and is as high as the highest price
        for part in ("var start", "raw_forecast[0].start", "var rows", "rows[i].value", "var maxP", "rows[j].start", "return out;"):
            assert part in zone, part
        assert all(".hour" not in js and ".price" not in js and "tomorrow_valid" not in js for js in generators)


@pytest.mark.parametrize("unit", ["ct/MWh", "c/MWh", "snt/kWh", "gr/kWh", "Rp/kWh", "GBX/kWh", "c€/kWh", "cts/kWh", "bani/kWh"])
def test_the_price_pages_refuse_the_subunits_also_per_mwh(unit) -> None:
    states = {"sensor.w": _State("100", {"unit_of_measurement": unit, "raw_today": []})}
    assert price_sensor_errors({"energypriceforecast_sell_entity_id": "sensor.w"}, states.get) == {"energypriceforecast_sell_entity_id": "price_unit_subunit"}


def test_a_price_per_mwh_is_converted_to_kwh_in_every_row() -> None:
    """A wholesale sensor often gives EUR/MWh: the rows are divided by 1000 (published, forecast, buy and sell); kWh is left as it is."""
    mwh = {"unit_of_measurement": "EUR/MWh"}
    attributes = {
        BUY: {**mwh, "raw_today": _rows(TODAY, lambda m: 100.0 + m.hour), "raw_tomorrow": _rows(TOMORROW, lambda m: 50.0), "raw_forecast": _rows(DAY_3, lambda m: 80.0)},
        SELL: {**mwh, "raw_today": _rows(TODAY, lambda m: 200.0), "raw_forecast": _rows(DAY_3, lambda m: 160.0)},
    }
    provider = _provider(attributes, energypriceforecast_sell_entity_id=SELL, sell_fee=0.05)
    assert provider.buy_real_prices[_at(8)] == pytest.approx(0.108)
    assert provider.buy_real_prices[_at(8, day=TOMORROW)] == pytest.approx(0.05)
    assert provider.buy_forecast_prices[_at(8, day=DAY_3)] == pytest.approx(0.08 + 2 / 30)  # the adder is per kWh
    assert provider.sell_real_prices[_at(8)] == pytest.approx(0.15)  # 0.2 less the fee
    kwh = _provider({BUY: {"unit_of_measurement": "EUR/kWh", "raw_today": _rows(TODAY, lambda m: 0.1)}})
    assert kwh.buy_real_prices[_at(8)] == 0.1
    # a sensor that says nothing about its unit is read as it is (kWh)
    assert _provider({BUY: {"raw_today": _rows(TODAY, lambda m: 0.1)}}).buy_real_prices[_at(8)] == 0.1


def test_a_price_per_mwh_gets_the_scale_of_its_currency_and_is_accepted_on_the_energy_price_forecast_page() -> None:
    states = {"sensor.w": _State("100", {"unit_of_measurement": "EUR/MWh", "raw_today": []})}
    assert detect_price_scale({"energypriceforecast_sell_entity_id": "sensor.w"}, states.get) == 0.134
    assert price_sensor_errors({"energypriceforecast_sell_entity_id": "sensor.w"}, states.get) == {}
    # Energi Data Service has its own price type (kWh): a price per MWh there is still refused
    assert price_sensor_errors({"energidataservice_buy_entity_id": "sensor.w"}, states.get) == {"energidataservice_buy_entity_id": "price_unit_subunit"}
    # and it is the same currency as a kWh sensor in EUR
    both = {"sensor.k": _State("0.1", {"unit_of_measurement": "EUR/kWh", "raw_today": []}), **states}
    assert price_currency_errors({"energypriceforecast_buy_entity_id": "sensor.k", "energypriceforecast_sell_entity_id": "sensor.w"}, ["energypriceforecast_sell_entity_id"], both.get) == {}


def test_the_chart_divides_the_rows_of_a_sensor_per_mwh_by_1000() -> None:
    """The chart reads the sensors' rows itself: a price per MWh is drawn per kWh like the planner's prices."""
    text = (TEMPLATE_DIR / "prices" / "energypriceforecast.yaml").read_text(encoding="utf-8")
    assert text.count("endsWith('mwh') ? 1000 : 1") == 6  # the three limits, the buy and the sell series and the forecast zone
    assert text.count("Number(row.value) / buyDiv") == 2 and "Number(row.value) / sellDiv" in text


def test_the_combined_provider_names_the_sensors_whose_history_is_per_mwh() -> None:
    """The recorded history of a sensor in EUR/MWh is divided by 1000 where it is read (the economy and the what-if)."""
    combined = create_price_provider(_config(energypriceforecast_sell_entity_id=SELL))
    states = {BUY: _State("0.2", {"unit_of_measurement": "EUR/kWh"}), SELL: _State("100", {"unit_of_measurement": "EUR/MWh"})}
    assert combined.price_unit_divisors(states.get) == {SELL: 1000.0}
    # a sensor that is not in Home Assistant has no known unit: its history is left out (None), never read as kWh
    assert combined.price_unit_divisors({}.get) == {BUY: None, SELL: None}
    other = create_price_provider(_config(energidataservice_buy_entity_id="sensor.eds"))
    # only Energy Price Forecast converts: a price per MWh of Energi Data Service is refused, its history is left out (None)
    assert other.price_unit_divisors({"sensor.eds": _State("0.2", {"unit_of_measurement": "EUR/MWh"}), BUY: states[BUY]}.get) == {"sensor.eds": None}


def test_the_unit_is_judged_while_the_sensor_is_down_but_the_retail_check_is_not() -> None:
    states = {"sensor.down": _State("unavailable", {"unit_of_measurement": "ct/kWh"}), "sensor.down2": _State("unknown", {"unit_of_measurement": "EUR/kWh", "raw_today": []})}
    assert price_sensor_errors({"energypriceforecast_buy_entity_id": "sensor.down"}, states.get) == {"energypriceforecast_buy_entity_id": "price_unit_subunit"}
    assert price_sensor_errors({"energypriceforecast_buy_entity_id": "sensor.down2"}, states.get) == {}


def test_currencies_are_compared_by_code_also_when_they_have_no_scale() -> None:
    states = {"sensor.czk": _State("2", {"unit_of_measurement": "CZK/kWh"}), "sensor.dkk": _State("1", {"unit_of_measurement": "DKK/kWh"}), "sensor.kr": _State("1", {"unit_of_measurement": "Kr/kWh"})}
    keys = ["energypriceforecast_buy_entity_id"]
    options = {"energidataservice_buy_entity_id": "sensor.dkk", "energypriceforecast_buy_entity_id": "sensor.czk"}
    assert price_currency_errors(options, keys, states.get) == {"energypriceforecast_buy_entity_id": "price_currency_mismatch"}
    assert price_currency_errors({**options, "energypriceforecast_buy_entity_id": "sensor.kr"}, keys, states.get) == {}  # kr is DKK


def test_published_prices_in_the_other_list_or_with_another_day_split_are_kept() -> None:
    """After midnight, before the integration rolls its lists over, today's rows are still in raw_tomorrow; and an
    integration that splits the days by its own market day puts an hour of tomorrow in raw_today."""
    first_of_tomorrow = _rows(TOMORROW, lambda m: 7.0)[:1]
    attributes = {BUY: {"raw_today": _rows(TODAY, lambda m: 1.0)[:20] + first_of_tomorrow, "raw_tomorrow": _rows(TODAY, lambda m: 9.0)[20:] + _rows(TOMORROW, lambda m: 2.0)}}
    prices = _provider(attributes).buy_real_prices
    assert all(_at(hour) in prices for hour in range(24)) and prices[_at(0, day=TOMORROW)] == 7.0  # the first row of a start wins
    assert prices[_at(21)] == 9.0 and prices[_at(10)] == 1.0
    # nothing older than today or later than tomorrow is a published price
    old = _rows(TODAY - datetime.timedelta(days=1), lambda m: 5.0)
    far = _rows(TODAY + datetime.timedelta(days=3), lambda m: 5.0)
    assert set(_provider({BUY: {"raw_today": old, "raw_tomorrow": far}}).buy_real_prices) == set()


@pytest.mark.parametrize("zone", ["Europe/London", "Europe/Helsinki", "Europe/Lisbon"])
def test_utc_rows_are_converted_to_the_time_zone_of_home_assistant(zone) -> None:
    tz = zoneinfo.ZoneInfo(zone)
    rows = []
    moment = datetime.datetime.combine(TODAY, datetime.time(0))
    while moment.date() == TODAY:
        rows.append({"start": moment.replace(tzinfo=tz).astimezone(UTC).isoformat().replace("+00:00", "Z"), "end": "", "value": float(moment.hour)})
        moment += datetime.timedelta(hours=1)
    prices = _provider({BUY: {"raw_today": rows}}, tz=tz).buy_real_prices
    assert prices == {_at(hour): float(hour) for hour in range(24)}


def test_the_energy_price_forecast_sensors_of_a_setup() -> None:
    prices = {"energypriceforecast_buy_entity_id": BUY, "energypriceforecast_sell_entity_id": SELL, "energidataservice_buy_entity_id": "sensor.eds"}
    assert energypriceforecast_entities(prices) == [BUY, SELL]
    assert energypriceforecast_entities({**prices, "energypriceforecast_sell_entity_id": BUY}) == [BUY]
    assert energypriceforecast_entities({"energypriceforecast_buy_entity_id": "", "energypriceforecast_sell_entity_id": None}) == []
    assert not is_forecast_only(None) and not is_forecast_only(_State("unknown", {"raw_forecast": [1]}))


def _eds(attributes: dict):
    provider = EnergiDataServicePriceProvider(build_config({**default_options(), "energidataservice_buy_entity_id": "sensor.eds"}))
    provider.shared_data = {"attributes": attributes, "values": {}, "version": 1}
    provider.now, provider.tz = NOW, COPENHAGEN
    provider.update_prices()
    return provider


def test_a_sensor_in_the_wrong_unit_gives_no_prices_while_running() -> None:
    """A price sensor that changed its unit after the setup (ct/kWh, p/kWh; per MWh for Energi Data Service) gives no prices
    at all: the next provider or the stored prices fill in, never a price 100 or 1000 times too high."""
    rows = _rows(TODAY, lambda m: 1.0)
    for unit in ("ct/kWh", "p/kWh", "GBp/kWh", "øre/kWh"):
        provider = _provider({BUY: {"unit_of_measurement": unit, "raw_today": rows}})
        assert provider.buy_real_prices == {} and provider.sell_real_prices == {}, unit
    assert _provider({BUY: {"unit_of_measurement": "EUR/kWh", "raw_today": rows}}).buy_real_prices[_at(8)] == 1.0
    assert _provider({BUY: {"unit_of_measurement": "EUR/MWh", "raw_today": rows}}).buy_real_prices[_at(8)] == 0.001  # converted, not refused

    eds_rows = [{"hour": _at(hour).isoformat(), "price": 2.0} for hour in range(24)]
    assert _eds({"sensor.eds": {"unit_of_measurement": "DKK/kWh", "raw_today": eds_rows}}).buy_real_prices[_at(8)] == 2.0
    for unit in ("DKK/MWh", "øre/kWh"):
        provider = _eds({"sensor.eds": {"unit_of_measurement": unit, "raw_today": eds_rows}})
        assert provider.buy_real_prices == {} and provider.sell_real_prices == {}, unit
    # a sensor that says nothing about its unit is read as it is (kWh)
    assert _eds({"sensor.eds": {"raw_today": eds_rows}}).buy_real_prices[_at(8)] == 2.0


def test_the_history_of_a_sensor_in_the_wrong_unit_is_left_out() -> None:
    combined = create_price_provider(_config())
    assert combined.price_unit_divisors({BUY: _State("25", {"unit_of_measurement": "ct/kWh"})}.get) == {BUY: None}
    eds = create_price_provider(build_config({**default_options(), "energidataservice_buy_entity_id": "sensor.eds"}))
    assert eds.price_unit_divisors({"sensor.eds": _State("2", {"unit_of_measurement": "DKK/MWh"})}.get) == {"sensor.eds": None}
    assert eds.price_unit_divisors({"sensor.eds": _State("2", {"unit_of_measurement": "DKK/kWh"})}.get) == {}
    assert eds.price_unit_divisors({"sensor.eds": _State("2", {"unit_of_measurement": "DKK/Wh"})}.get) == {"sensor.eds": None}
    assert eds.price_unit_divisors({}.get) == {}  # a sensor that is not there: Energi Data Service keeps its history as it was


def test_the_economy_sell_history_follows_the_sell_sensor_the_live_prices_use() -> None:
    """Without a sell sensor the live prices sell at the buy sensor's price, and the history the what-if reads is its history."""
    assert create_price_provider(_config()).sell_price_entity_ids() == [BUY]
    assert create_price_provider(_config(energypriceforecast_sell_entity_id=SELL)).sell_price_entity_ids() == [SELL]
    eds = create_price_provider(build_config({**default_options(), "energidataservice_buy_entity_id": "sensor.eds"}))
    assert eds.sell_price_entity_ids() == ["sensor.eds"]


def test_the_forecast_and_a_separate_sell_sensor_in_the_wrong_unit_give_no_prices() -> None:
    forecast = _rows(DAY_3, lambda m: 1.0)
    assert _provider({BUY: {"unit_of_measurement": "ct/kWh", "raw_forecast": forecast}}).buy_forecast_prices == {}
    assert _provider({BUY: {"unit_of_measurement": "EUR/kWh", "raw_forecast": forecast}}).buy_forecast_prices  # the same rows in the right unit
    rows = _rows(TODAY, lambda m: 1.0)
    separate = _provider(
        {BUY: {"unit_of_measurement": "EUR/kWh", "raw_today": rows}, SELL: {"unit_of_measurement": "ct/kWh", "raw_today": rows}},
        energypriceforecast_sell_entity_id=SELL,
    )
    assert separate.buy_real_prices and separate.sell_real_prices == {}  # the buy prices stay
    eds_forecast = [{"hour": _at(hour, day=DAY_3).isoformat(), "price": 2.0} for hour in range(24)]
    assert _eds({"sensor.eds": {"unit_of_measurement": "DKK/kWh", "forecast": eds_forecast}}).buy_forecast_prices
    for unit in ("DKK/MWh", "DKK/Wh"):
        assert _eds({"sensor.eds": {"unit_of_measurement": unit, "forecast": eds_forecast}}).buy_forecast_prices == {}, unit


def test_energi_data_service_per_wh_is_refused_too() -> None:
    """A price per Wh is 1000 times too small: it rounds to 0 and would look like free electricity."""
    states = {"sensor.w": _State("0.002", {"unit_of_measurement": "DKK/Wh", "raw_today": []})}
    assert price_sensor_errors({"energidataservice_buy_entity_id": "sensor.w"}, states.get) == {"energidataservice_buy_entity_id": "price_unit_subunit"}
    assert price_sensor_errors({"energypriceforecast_sell_entity_id": "sensor.w"}, {}.get) == {}
    eds_rows = [{"hour": _at(hour).isoformat(), "price": 0.002} for hour in range(24)]
    assert _eds({"sensor.eds": {"unit_of_measurement": "DKK/Wh", "raw_today": eds_rows}}).buy_real_prices == {}


def test_no_prices_at_all_is_no_error_and_a_wrong_unit_on_the_sell_sensor_keeps_the_buy_prices() -> None:
    """Every sensor in the wrong unit: an empty result, not a ValueError (the engine plans on the cached prices or the price
    history). The prices of the other kind stay."""
    rows = _rows(TODAY, lambda m: 1.0)
    combined = create_price_provider(_config(energypriceforecast_sell_entity_id=SELL))
    combined.set_now(NOW, COPENHAGEN)
    assert combined.prices_loaded() is False
    combined.set_snapshot({"attributes": {BUY: {"unit_of_measurement": "ct/kWh", "raw_today": rows}, SELL: {"unit_of_measurement": "ct/kWh", "raw_today": rows}}, "values": {}})
    assert combined.prices_loaded() is True
    assert combined.get_buy_prices() == {} and combined.get_sell_prices() == {} and combined.buy_prices_available() is False
    combined.set_snapshot({"attributes": {BUY: {"unit_of_measurement": "EUR/kWh", "raw_today": rows}, SELL: {"unit_of_measurement": "ct/kWh", "raw_today": rows}}, "values": {}})
    assert combined.buy_prices_available() is True and combined.get_buy_prices() and combined.get_sell_prices() == {}
