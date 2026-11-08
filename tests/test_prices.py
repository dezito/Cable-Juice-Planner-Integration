"""The combined price provider: priority, snapshot, history and prices, and the sell price."""
from __future__ import annotations

import datetime
import math

import pytest

from custom_components.cable_juice_planner.const import build_config, default_options
from custom_components.cable_juice_planner.prices import (
    EnergiDataServicePriceProvider,
    StromligningPriceProvider,
    create_price_provider,
)
from custom_components.cable_juice_planner.prices.base import BasePriceProvider, sell_adder_sign

TODAY = datetime.date(2026, 10, 30)
TOMORROW = TODAY + datetime.timedelta(days=1)


class _State:
    def __init__(self, state: str, attributes: dict | None = None) -> None:
        self.state = state
        self.attributes = attributes or {}


def _starts(day: datetime.date) -> list[str]:
    return [datetime.datetime.combine(day, datetime.time(hour)).isoformat() + "+01:00" for hour in range(24)]


def _both_providers():
    return create_price_provider(build_config({
        **default_options(),
        "energidataservice_buy_entity_id": "sensor.eds",
        "energidataservice_sell_entity_id": "sensor.eds_sell",
        "stromligning_buy_current_price": "sensor.stromligning",
        "stromligning_sell_nettariff": "sensor.sell_nettariff",
    }))


def test_stromligning_has_priority_over_energi_data_service() -> None:
    provider = _both_providers()
    assert [type(p) for p in provider.providers] == [StromligningPriceProvider, EnergiDataServicePriceProvider]
    assert provider.buy_price_entity_ids() == ["sensor.stromligning", "sensor.eds"]
    # recorded history: per hour Strømligning first, Energi Data Service fills the gaps
    assert provider.combine_history({
        "sensor.eds": {1.0: 3.0, 2.0: 3.0},
        "sensor.stromligning": {1.0: 1.0},
        "sensor.other": {3.0: 9.0},
    }) == {1.0: 1.0, 2.0: 3.0}


def test_combined_provider_reads_the_price_entities_and_merges_the_prices() -> None:
    provider = _both_providers()
    eds = {
        "raw_today": [{"hour": start, "price": 3.0} for start in _starts(TODAY)],
        "forecast": [{"hour": start, "price": 2.0} for start in _starts(TOMORROW)],
    }
    states = {
        # Strømligning only has the first 12 hours
        "sensor.stromligning": _State("2.0", {"prices": [{"start": start, "price": 1.0} for start in _starts(TODAY)[:12]]}),
        "sensor.eds": _State("3.0", eds),
        "sensor.eds_sell": _State("3.0", eds),
        "sensor.sell_nettariff": _State("0.05"),
    }
    snapshot = provider.snapshot(states.get)
    assert set(snapshot["attributes"]) == {"sensor.stromligning", "sensor.eds", "sensor.eds_sell", "sensor.sell_nettariff"}
    # only the tariff sensors are read as a state
    assert snapshot["values"] == {"sensor.sell_nettariff": 0.05}

    now = datetime.datetime.combine(TODAY, datetime.time(10, 30))
    provider.set_now(now)
    provider.set_snapshot(snapshot)
    morning, evening = (datetime.datetime.combine(TODAY, datetime.time(hour)) for hour in (8, 20))
    assert provider.get_buy_price(now) == 1.0
    assert provider.get_buy_prices()[morning] == 1.0 and provider.price_source(morning) == "stromligning:real"
    assert provider.get_buy_prices()[evening] == 3.0 and provider.price_source(evening) == "energidataservice:real"
    tomorrow = datetime.datetime.combine(TOMORROW, datetime.time(8))
    assert provider.is_forecast(tomorrow)
    assert provider.get_buy_forecast_prices()[tomorrow] > provider.get_buy_forecast_prices(adder=False)[tomorrow] == pytest.approx(2.0, abs=0.005)
    assert tomorrow not in provider.get_buy_real_prices()
    assert provider.get_current_price_timestamp(now) == datetime.datetime.combine(TODAY, datetime.time(10))
    assert provider.get_buy_price_entity_id() == "sensor.stromligning, sensor.eds"


# --- the sell price: what selling a kWh costs is subtracted (same formula as BattMind) ---

NOW = datetime.datetime.combine(TODAY, datetime.time(16, 40))
# Strømligning tariffs ex VAT (a real export): distribution per period, the rest flat
DISTRIBUTION = {hour: 0.9358 if 17 <= hour <= 20 else 0.3119 for hour in range(24)}
FLAT_TARIFFS = {
    "sensor.sell_tax": 0.008,
    "sensor.sell_nettariff": 0.043,
    "sensor.sell_systemtariff": 0.072,
    "sensor.sell_surcharge": 0.04,
}
# not on a rounding boundary after the selling costs are subtracted
SPOT = {TODAY: 2.611, TOMORROW: 2.213}


def _config(**options):
    return build_config({**default_options(), **options})


def _load(provider: BasePriceProvider, attributes: dict, values: dict | None = None) -> BasePriceProvider:
    provider.shared_data = {"attributes": attributes, "values": values or {}, "version": 1}
    provider.now = NOW
    provider.update_prices()
    return provider


def _rows(day: datetime.date, price) -> list[dict]:
    return [{"start": start, "price": price(hour)} for hour, start in enumerate(_starts(day))]


def _quarter_rows(day: datetime.date, price) -> list[dict]:
    midnight = datetime.datetime.combine(day, datetime.time(0))
    return [
        {"start": (midnight + datetime.timedelta(minutes=15 * index)).isoformat() + "+01:00", "price": price(index // 4)}
        for index in range(96)
    ]


def _sell_with_tariffs(day: datetime.date):
    """Strømligning's sell sensor is the consumer price: spot plus every tariff."""
    return lambda hour: SPOT[day] + DISTRIBUTION[hour] + sum(FLAT_TARIFFS.values())


def _stromligning(sell_fee: float = 0.03, quarter: bool = False) -> StromligningPriceProvider:
    """quarter: 15-minute sell prices (the distribution tariff stays per hour)."""
    sell_rows = _quarter_rows if quarter else _rows
    provider = StromligningPriceProvider(_config(**{
        "stromligning_buy_current_price": "sensor.buy",
        "stromligning_sell_current_price": "sensor.sell",
        "stromligning_sell_tomorrow_available": "binary_sensor.sell_tomorrow",
        "stromligning_sell_distribution": "sensor.sell_distribution",
        "stromligning_sell_electricity_tax": "sensor.sell_tax",
        "stromligning_sell_nettariff": "sensor.sell_nettariff",
        "stromligning_sell_systemtariff": "sensor.sell_systemtariff",
        "stromligning_sell_surcharge": "sensor.sell_surcharge",
        "sell_fee": sell_fee,
    }))
    _load(
        provider,
        {
            "sensor.buy": {"prices": _rows(TODAY, lambda hour: 4.0)},
            "sensor.sell": {"prices": sell_rows(TODAY, _sell_with_tariffs(TODAY))},
            "binary_sensor.sell_tomorrow": {"forecast_data": False, "prices": sell_rows(TOMORROW, _sell_with_tariffs(TOMORROW))},
            # like the real sensor: only today's distribution tariffs
            "sensor.sell_distribution": {"prices": _rows(TODAY, DISTRIBUTION.get)},
            **{entity_id: {} for entity_id in FLAT_TARIFFS},
        },
        dict(FLAT_TARIFFS),
    )
    return provider


@pytest.mark.parametrize("day", [TODAY, TOMORROW])
@pytest.mark.parametrize("hour", [3, 17, 20, 21])
def test_stromligning_sell_price_is_spot_minus_the_selling_costs(day: datetime.date, hour: int) -> None:
    """Every tariff in the sell sensor is subtracted (also tomorrow's distribution tariff, which the
    sensor doesn't have yet), and selling costs the sell fee plus Energinet's net and system tariff."""
    provider = _stromligning()
    timestamp = datetime.datetime.combine(day, datetime.time(hour))
    assert provider.sell_real_prices[timestamp] == pytest.approx(round(SPOT[day] - 0.03 - 0.043 - 0.072, 2), abs=1e-9)


def test_stromligning_tariffs_use_the_keys_of_energi_data_service() -> None:
    """The distribution tariff is the grid company's `tariffs`, the net tariff Energinet's `transmissions_nettarif`."""
    provider = _stromligning()
    tariffs = provider.get_tariffs(17, 4, timestamp=datetime.datetime.combine(TODAY, datetime.time(17)))
    assert tariffs["tariffs"] == pytest.approx(0.9358)
    assert tariffs["transmissions_nettarif"] == pytest.approx(0.043)
    assert tariffs["systemtarif"] == pytest.approx(0.072)
    assert tariffs["elafgift"] == pytest.approx(0.008)
    assert tariffs["surcharge"] == pytest.approx(0.04)
    assert tariffs["tariff_sum"] == pytest.approx(0.9358 + 0.043 + 0.072 + 0.008 + 0.04)


class _Provider(BasePriceProvider):
    def __init__(self, config: dict, tariffs: dict, sell_costs: dict | None = None) -> None:
        super().__init__(config)
        self._tariffs = tariffs
        self._sell_costs = sell_costs

    def get_tariffs(self, hour, day_of_week, timestamp=None) -> dict:
        return self._tariffs

    def get_sell_costs(self, hour, day_of_week, timestamp=None):
        return self._sell_costs


def test_selling_costs_lower_the_sell_price() -> None:
    tariffs = {"transmissions_nettarif": 0.1, "systemtarif": 0.1, "elafgift": 0.5, "tariffs": 0.2, "surcharge": 0.0, "tariff_sum": 0.9}
    timestamp = datetime.datetime.combine(TODAY, datetime.time(17))
    # 2.0 - 0.9 tariffs in the price - (0.03 fee + 0.1 net + 0.1 system tariff)
    provider = _Provider(_config(sell_fee=0.03), tariffs)
    assert provider.calculate_sell_price(timestamp, 2.0) == pytest.approx(0.87)
    assert provider.sell_price_details[timestamp]["sell_tariffs"] == pytest.approx(0.23)
    # the fee is stored positive and always subtracted
    assert _Provider(_config(sell_fee=-0.03), tariffs).calculate_sell_price(timestamp, 2.0) == pytest.approx(0.87)
    # a raw price without tariffs: only the selling costs
    raw = {key: 0.0 for key in tariffs}
    provider = _Provider(_config(sell_fee=0.03), raw, {"transmissions_nettarif": 0.05, "systemtarif": 0.09})
    assert provider.calculate_sell_price(timestamp, 2.0) == pytest.approx(1.83)
    details = provider.sell_price_details[timestamp]
    assert details["sell_tariffs"] == pytest.approx(0.17)
    # what was subtracted for selling, for the attributes of the solar price
    assert (details["sell_transmissions_nettarif"], details["sell_systemtarif"]) == pytest.approx((0.05, 0.09))


# --- a price that is not a finite number never reaches the plan (same rule as BattMind) ---

NOT_PRICES = [float("nan"), float("inf"), float("-inf"), None, "abc", True]


@pytest.mark.parametrize("bad", NOT_PRICES, ids=repr)
def test_energi_data_service_skips_rows_without_a_finite_price(bad) -> None:
    provider = EnergiDataServicePriceProvider(_config(energidataservice_buy_entity_id="sensor.eds"))
    today = [{"hour": start, "price": 2.5} for start in _starts(TODAY)]
    today[5] = {"hour": today[5]["hour"], "price": bad}
    forecast = [{"hour": start, "price": 2.0} for start in _starts(TODAY + datetime.timedelta(days=2))]
    forecast[7] = {"hour": forecast[7]["hour"], "price": bad}
    _load(provider, {"sensor.eds": {"raw_today": today, "tomorrow_valid": False, "forecast": forecast}})
    real, predicted = provider.get_buy_real_prices(), provider.get_buy_forecast_prices()
    assert datetime.datetime.combine(TODAY, datetime.time(5)) not in real and len(real) == 23
    assert datetime.datetime.combine(TODAY + datetime.timedelta(days=2), datetime.time(7)) not in predicted and len(predicted) == 23
    assert all(math.isfinite(price) for price in (*real.values(), *predicted.values()))


@pytest.mark.parametrize("bad", NOT_PRICES, ids=repr)
def test_stromligning_skips_rows_without_a_finite_price(bad) -> None:
    provider = StromligningPriceProvider(_config(stromligning_buy_current_price="sensor.buy"))
    rows = _rows(TODAY, lambda hour: bad if hour == 5 else 1.5)
    _load(provider, {"sensor.buy": {"prices": rows}})
    prices = provider.get_buy_real_prices()
    assert datetime.datetime.combine(TODAY, datetime.time(5)) not in prices and len(prices) == 23
    assert all(math.isfinite(price) for price in prices.values())


def test_the_combined_prices_drop_a_price_that_is_not_a_finite_number() -> None:
    """Whatever a provider returns: such an hour is missing (filled from the price history), never a price."""
    five, six = (datetime.datetime.combine(TODAY, datetime.time(hour)) for hour in (5, 6))

    class _Stub:
        source_name = "stub"
        sell_price_details: dict = {}

        def get_buy_real_prices(self):
            return {five: float("nan"), six: 1.0}

        def get_buy_forecast_prices(self):
            return {five + datetime.timedelta(days=2): float("-inf"), six + datetime.timedelta(days=2): 2.0}

    provider = _both_providers()
    provider.providers = [_Stub()]
    prices, real, sources, _ = provider._combine("buy")
    assert prices == {six: 1.0, six + datetime.timedelta(days=2): 2.0}
    assert real == {six: 1.0} and set(sources) == set(prices)


def _eds_provider(sell_tariffs: bool) -> EnergiDataServicePriceProvider:
    provider = EnergiDataServicePriceProvider(_config(**{
        "energidataservice_buy_entity_id": "sensor.eds_buy",
        "energidataservice_sell_entity_id": "sensor.eds_sell",
        "sell_fee": 0.03,
    }))
    tariffs = {
        "additional_tariffs": {"transmissions_nettarif": 0.054, "systemtarif": 0.09, "elafgift": 0.01},
        "tariffs": {str(hour): 1.17 if 17 <= hour <= 20 else 0.39 for hour in range(24)},
    }
    rows = [{"hour": start, "price": 2.5} for start in _starts(TODAY)]
    sell = {"raw_today": rows, "tomorrow_valid": False}
    if sell_tariffs:
        sell["tariffs"] = tariffs
    _load(provider, {"sensor.eds_buy": {"raw_today": rows, "tomorrow_valid": False, "tariffs": tariffs}, "sensor.eds_sell": sell})
    return provider


def test_raw_energi_data_service_sell_price_gets_the_selling_costs_from_the_buy_sensor() -> None:
    """The sell sensor without tariffs is the spot price: only fee, net and system tariff are subtracted."""
    timestamp = datetime.datetime.combine(TODAY, datetime.time(17))
    raw = _eds_provider(False)
    assert raw.sell_real_prices[timestamp] == pytest.approx(round(2.5 - 0.03 - 0.054 - 0.09, 2), abs=1e-9)
    assert raw.sell_price_details[timestamp]["sell_tariffs"] == pytest.approx(0.03 + 0.054 + 0.09)
    # with tariffs in the sell sensor they are subtracted, plus the selling costs
    with_tariffs = _eds_provider(True)
    assert with_tariffs.sell_real_prices[timestamp] == pytest.approx(round(2.5 - 1.324 - 0.03 - 0.054 - 0.09, 2), abs=1e-9)


def test_energi_data_service_without_any_tariffs_sells_for_the_price_less_the_fee() -> None:
    """One sensor for buying and selling with the tariffs switched off: nothing to subtract but the sell fee."""
    provider = EnergiDataServicePriceProvider(_config(energidataservice_buy_entity_id="sensor.eds", sell_fee=0.03))
    rows = [{"hour": start, "price": 2.5} for start in _starts(TODAY)]
    _load(provider, {"sensor.eds": {"raw_today": rows, "tomorrow_valid": False}})
    assert provider.sell_real_prices[datetime.datetime.combine(TODAY, datetime.time(17))] == pytest.approx(2.47)


@pytest.mark.parametrize("day", [TODAY, TOMORROW])
def test_quarter_hour_sell_prices_use_the_hourly_distribution_tariff(day: datetime.date) -> None:
    """Every 15 minutes of an hour get that hour's distribution tariff, also tomorrow's (from today's periods)."""
    provider = _stromligning(quarter=True)
    for hour, minute in ((3, 15), (17, 0), (17, 45), (21, 30)):
        timestamp = datetime.datetime.combine(day, datetime.time(hour, minute))
        assert provider.sell_real_prices[timestamp] == pytest.approx(round(SPOT[day] - 0.03 - 0.043 - 0.072, 2), abs=1e-9)


@pytest.mark.parametrize(("fixed", "sign"), [(-1.0, -1.0), (1.0, 1.0)], ids=["hourly sell price", "fixed solar sell price"])
def test_forecast_adder_makes_a_forecast_buy_dearer_and_a_forecast_sale_worth_less(fixed: float, sign: float) -> None:
    """A forecast price gets 1/30 per day ahead to prefer known prices: buying costs more, selling earns less
    (with a fixed solar sell price a sold kWh is worth fixed - hourly, so there the hourly price goes up)."""
    forecast_day = TODAY + datetime.timedelta(days=2)
    timestamp = datetime.datetime.combine(forecast_day, datetime.time(12))
    provider = StromligningPriceProvider(_config(**{
        "stromligning_buy_current_price": "sensor.buy",
        "stromligning_buy_forecasts": "sensor.buy_forecast",
        "stromligning_sell_forecasts": "sensor.sell_forecast",
        "sell_fee": 0.03,
        "solar_sell_fixed_price": fixed,
    }))
    _load(provider, {
        "sensor.buy": {"prices": _rows(TODAY, lambda hour: 4.0)},
        "sensor.buy_forecast": {"prices": _rows(forecast_day, lambda hour: 1.5)},
        "sensor.sell_forecast": {"prices": _rows(forecast_day, lambda hour: 1.5)},
    })
    assert provider.buy_forecast_prices[timestamp] == pytest.approx(1.5 + 2 / 30)
    assert provider.sell_forecast_prices[timestamp] == pytest.approx(round(1.5 + sign * 2 / 30 - 0.03, 2), abs=1e-9)


@pytest.mark.parametrize(("fixed", "sign"), [(-1.0, -1.0), (1.0, 1.0)], ids=["hourly sell price", "fixed solar sell price"])
def test_forecast_in_the_tomorrow_entity_gets_the_same_adder(fixed: float, sign: float) -> None:
    """Strømligning's tomorrow entity holds forecasts (forecast_data) until the real prices are published."""
    tomorrow = TODAY + datetime.timedelta(days=1)
    timestamp = datetime.datetime.combine(tomorrow, datetime.time(12))
    provider = StromligningPriceProvider(_config(**{
        "stromligning_buy_current_price": "sensor.buy",
        "stromligning_buy_tomorrow_available": "binary_sensor.buy_tomorrow",
        "stromligning_sell_tomorrow_available": "binary_sensor.sell_tomorrow",
        "sell_fee": 0.03,
        "solar_sell_fixed_price": fixed,
    }))
    forecast = {"forecast_data": True, "prices": _rows(tomorrow, lambda hour: 1.5)}
    _load(provider, {
        "sensor.buy": {"prices": _rows(TODAY, lambda hour: 4.0)},
        "binary_sensor.buy_tomorrow": forecast,
        "binary_sensor.sell_tomorrow": forecast,
    })
    assert provider.buy_forecast_prices[timestamp] == pytest.approx(1.5 + 1 / 30)
    assert provider.sell_forecast_prices[timestamp] == pytest.approx(round(1.5 + sign / 30 - 0.03, 2), abs=1e-9)


@pytest.mark.parametrize(("production_price", "sign"), [(-1, -1.0), (-1.0, -1.0), ("-1", -1.0), (0.85, 1.0), ("0.85", 1.0), (0, 1.0), ("", -1.0), (None, -1.0)])
def test_sell_adder_sign_reads_the_fixed_solar_sell_price(production_price, sign: float) -> None:
    """-1 = no fixed price; anything that isn't a number counts as no fixed price (the buy prices must still work)."""
    assert sell_adder_sign({"solar": {"production_price": production_price}}) == sign


@pytest.mark.parametrize(("fixed", "sign"), [(-1.0, -1.0), (1.0, 1.0)], ids=["hourly sell price", "fixed solar sell price"])
def test_combined_sell_forecast_without_the_adder(fixed: float, sign: float) -> None:
    """adder=False removes the penalty with the sign it was added with."""
    forecast_day = TODAY + datetime.timedelta(days=2)
    timestamp = datetime.datetime.combine(forecast_day, datetime.time(12))
    provider = create_price_provider(build_config({
        **default_options(),
        "energidataservice_buy_entity_id": "sensor.eds",
        "energidataservice_sell_entity_id": "sensor.eds_sell",
        "sell_fee": 0.0,
        "solar_sell_fixed_price": fixed,
    }))
    eds = {
        "raw_today": [{"hour": start, "price": 3.0} for start in _starts(TODAY)],
        "forecast": [{"hour": start, "price": 1.5} for start in _starts(forecast_day)],
    }
    states = {"sensor.eds": _State("3.0", eds), "sensor.eds_sell": _State("3.0", eds)}
    provider.set_now(datetime.datetime.combine(TODAY, datetime.time(10, 30)))
    provider.set_snapshot(provider.snapshot(states.get))
    with_adder = provider.get_sell_forecast_prices()[timestamp]
    without = provider.get_sell_forecast_prices(adder=False)[timestamp]
    assert with_adder == pytest.approx(1.5 + sign * 2 / 30, abs=0.006)
    assert without == pytest.approx(1.5, abs=0.011)
