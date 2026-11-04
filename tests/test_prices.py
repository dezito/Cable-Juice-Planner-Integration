"""The combined price provider: priority, snapshot, history and prices."""
from __future__ import annotations

import datetime

import pytest

from custom_components.cable_juice_planner.const import build_config, default_options
from custom_components.cable_juice_planner.prices import (
    EnergiDataServicePriceProvider,
    StromligningPriceProvider,
    create_price_provider,
)

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
