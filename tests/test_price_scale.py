"""Prices in another currency: the money limits of the planner follow prices.price_scale (kroner = 1.0)."""
from __future__ import annotations

import datetime
import math

import pytest

from custom_components.cable_juice_planner.const import build_config, default_options, price_decimals, price_scale
from custom_components.cable_juice_planner.prices import create_price_provider, detect_price_scale
from custom_components.cable_juice_planner.prices.base import sell_price_parts
from custom_components.cable_juice_planner.prices.currency import price_scale_of_unit

TODAY = datetime.date(2026, 10, 30)
TOMORROW = TODAY + datetime.timedelta(days=1)
NOW = datetime.datetime.combine(TODAY, datetime.time(10, 30))


class _State:
    def __init__(self, state: str, attributes: dict | None = None) -> None:
        self.state = state
        self.attributes = attributes or {}


def _starts(day: datetime.date) -> list[str]:
    return [datetime.datetime.combine(day, datetime.time(hour)).isoformat() + "+01:00" for hour in range(24)]


def _provider(scale: float | None = None):
    options = {**default_options(), "energidataservice_buy_entity_id": "sensor.eds"}
    if scale is not None:
        options["price_scale"] = scale
    return create_price_provider(build_config(options))


def _loaded(provider, today_price: float, forecast_price: float = 0.1):
    eds = {
        "raw_today": [{"hour": start, "price": today_price} for start in _starts(TODAY)],
        "forecast": [{"hour": start, "price": forecast_price} for start in _starts(TOMORROW)],
    }
    provider.set_now(NOW)
    provider.set_snapshot(provider.snapshot({"sensor.eds": _State("1.0", eds)}.get))
    return provider


def test_a_missing_or_invalid_scale_is_kroner() -> None:
    assert price_scale(build_config(default_options())) == 1.0
    assert price_scale({}) == 1.0 and price_scale({"prices": {}}) == 1.0  # an entry made before the option existed
    for invalid in (0, -1.0, math.nan, math.inf, "0.134", None, True):
        assert price_scale({"prices": {"price_scale": invalid}}) == 1.0, invalid
    assert price_scale({"prices": {"price_scale": 0.134}}) == 0.134
    assert price_decimals({}) == 2 and price_decimals({"prices": {"price_scale": 1.4}}) == 2
    assert price_decimals({"prices": {"price_scale": 0.134}}) == 3  # EUR


@pytest.mark.parametrize(
    ("unit", "scale"),
    [
        ("DKK/kWh", 1.0), ("kr/kWh", 1.0), ("kr./kWh", 1.0), (" dkk / kWh ", 1.0), ("SEK/kWh", 1.4), ("NOK/kWh", 1.5),
        ("EUR/kWh", 0.134), ("€/kWh", 0.134), ("GBP/kWh", 0.115), ("£/kWh", 0.115), ("PLN/kWh", 0.57), ("zł/kWh", 0.57),
        # not a known currency per kWh: not guessed
        ("EUR/MWh", 0.134), ("ct/kWh", None), ("GBp/kWh", None), ("Kr/kWh", 1.0), ("Zł/kWh", 0.57), ("Eur/kWh", 0.134), ("Gbp/kWh", 0.115), ("p/kWh", None), ("c/kWh", None), ("EURct/kWh", None), ("EUR", None), ("XYZ/kWh", None), ("", None), (None, None), (5, None),
    ],
)
def test_the_scale_of_a_price_unit(unit, scale) -> None:
    assert price_scale_of_unit(unit) == scale


def test_the_scale_is_found_from_the_price_sensors_not_from_home_assistants_currency() -> None:
    states = {
        "sensor.eds": _State("0.5", {"unit_of_measurement": "EUR/kWh"}),
        "sensor.sl": _State("1.0", {"unit_of_measurement": "DKK/kWh"}),
        "sensor.none": _State("1.0", {}),
    }
    assert detect_price_scale({"energidataservice_buy_entity_id": "sensor.eds"}, states.get) == 0.134
    # Strømligning comes first in the priority of the providers
    both = {"energidataservice_buy_entity_id": "sensor.eds", "stromligning_buy_current_price": "sensor.sl"}
    assert detect_price_scale(both, states.get) == 1.0
    # a sensor without a unit, missing or not loaded: the next one, else None
    assert detect_price_scale({**both, "stromligning_buy_current_price": "sensor.none"}, states.get) == 0.134
    assert detect_price_scale({"energidataservice_buy_entity_id": "sensor.none"}, states.get) is None
    assert detect_price_scale({"energidataservice_buy_entity_id": "sensor.missing"}, states.get) is None
    assert detect_price_scale({}, states.get) is None


def test_the_forecast_adder_follows_the_scale() -> None:
    tomorrow = datetime.datetime.combine(TOMORROW, datetime.time(8))
    for scale, expected in ((None, 1 / 30), (1.0, 1 / 30), (0.134, 0.134 / 30)):
        provider = _loaded(_provider(scale), 0.5)
        assert provider.providers[0]._forecast_adder(tomorrow) == expected  # exactly: kroner are unchanged
        assert provider.providers[0]._forecast_adder(tomorrow, sell=True) == -expected


def test_the_adder_taken_off_a_forecast_price_follows_the_scale_too() -> None:
    tomorrow = datetime.datetime.combine(TOMORROW, datetime.time(8))
    provider = _loaded(_provider(0.134), 0.5)
    with_adder = provider.get_buy_forecast_prices()[tomorrow]
    without = provider.get_buy_forecast_prices(adder=False)[tomorrow]
    assert with_adder - without == pytest.approx(0.134 / 30, abs=0.0006)  # (3 decimals)
    assert without == pytest.approx(0.1, abs=0.002)


def test_prices_are_rounded_to_the_decimals_of_the_currency() -> None:
    hour = datetime.datetime.combine(TODAY, datetime.time(10))
    assert _loaded(_provider(), 0.12345).get_buy_prices()[hour] == 0.12  # kroner: as before
    assert _loaded(_provider(1.0), 0.12345).get_buy_prices()[hour] == 0.12
    assert _loaded(_provider(0.134), 0.12345).get_buy_prices()[hour] == 0.123


@pytest.mark.parametrize(("scale", "expected"), [(None, 0.12), (0.134, 0.123)])
def test_the_recorded_sell_price_is_rounded_to_the_decimals_of_the_currency(scale, expected) -> None:
    """What-if solar: the price of the history with the tariffs of its day (EDS) has the decimals of the live sell price."""
    provider = _provider(scale).providers[0]
    day = {"tariffs": None, "sell_costs": {"transmissions_nettarif": 0.0, "systemtarif": 0.0}, "sell_fee": 0.0}
    assert provider.sell_price_from_tariff_day(datetime.datetime.combine(TODAY, datetime.time(10)), 0.12345, day) == expected


@pytest.mark.parametrize(("scale", "expected"), [(None, 0.15), (0.134, 0.127)])
def test_prices_from_the_history_get_the_adder_and_decimals_of_the_currency(scale, expected) -> None:
    """The price history gives 0.123456 for the hour; a day ahead it gets 1/30 (kroner) or 0.134/30 (EUR) and the decimals of the currency."""
    from custom_components.cable_juice_planner import const
    from custom_components.cable_juice_planner.engine import CJPEngine

    from .test_engine import DAY, OPTIONS, TEXTS, snapshot

    now = datetime.datetime.combine(DAY, datetime.time(10, 5))
    snap = snapshot(now)
    engine = CJPEngine(const.build_config({**OPTIONS, **({"price_scale": scale} if scale else {})}), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, {}).error is None
    tomorrow_noon = datetime.datetime.combine(DAY + datetime.timedelta(days=1), datetime.time(12))
    engine.kwh_avg_prices_db["history"] = {12: {tomorrow_noon.weekday(): [0.123456]}}
    result = engine.run_job(snap, engine._database_prices, "history", {}, 3)
    assert result.error is None, result.error
    assert result.value[tomorrow_noon] == expected


def test_a_restored_backup_or_an_import_keeps_its_sell_fee_and_only_a_new_car_gets_the_suggestion() -> None:
    from types import SimpleNamespace

    from custom_components.cable_juice_planner.flow.entry import CJPConfigFlow

    states = {"sensor.eds": _State("0.5", {"unit_of_measurement": "EUR/kWh"})}

    def flow(**attributes) -> CJPConfigFlow:
        new = CJPConfigFlow()
        new.hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
        new._options = {**default_options(), "energidataservice_buy_entity_id": "sensor.eds"}
        for key, value in attributes.items():
            setattr(new, key, value)
        return new

    new = flow()
    new._apply_price_scale()  # the finish of the flow: the fee has been seen on its page already
    assert new._options["price_scale"] == 0.134 and new._options["sell_fee"] == 0.03
    new = flow()
    new._apply_price_scale(suggest_fee=True)
    assert new._options["sell_fee"] == 0.004
    for own in ({"_restore_backup": "backup"}, {"_legacy_prefix": "ev"}):
        kept = flow(**own)
        kept._apply_price_scale(suggest_fee=True)
        assert kept._options["price_scale"] == 0.134 and kept._options["sell_fee"] == 0.03, own  # a stored 0.03 is not the default


def test_the_sell_price_is_rounded_to_the_decimals_of_the_currency() -> None:
    tariff = {"tariff_sum": 0.0}
    costs = {"transmissions_nettarif": 0.0, "systemtarif": 0.0}
    assert sell_price_parts(0.12345, tariff, costs, 0.0)[2] == 0.12
    assert sell_price_parts(0.12345, tariff, costs, 0.0, 3)[2] == 0.123
