"""What-if Scenario B: two sets of CJP settings replayed over the same recorded demand."""
from __future__ import annotations

import math
from dataclasses import replace

import pytest

from custom_components.cable_juice_planner.engine.whatif.replay import (
    CjpSettings,
    Event,
    replay,
    replay_settings,
    settings_target,
)

from .test_whatif_replay import LOSS, flat, hour, make

LIVE = CjpSettings(reserve=30.0, maximum=90.0)


def drive(at: float, percent: float) -> Event:
    return Event(ts=hour(at), kind="drive", soc_percent=percent)


def three_days(prices=None, **kwargs):
    """Three nightly connections with a drive of 20 % after each of the first two."""
    windows = [(0, 8), (20, 32), (44, 56)]
    events = [drive(12, 20.0), drive(36, 20.0)]
    return make(windows, prices if prices is not None else flat(60), events=events, start=50.0, **kwargs)


# --------------------------------------------------------------------------- target rule


def test_the_target_is_the_need_plus_the_reserve_capped_at_the_maximum() -> None:
    assert settings_target(20.0, LIVE) == pytest.approx(50.0)
    assert settings_target(0.0, LIVE) == pytest.approx(30.0)  # nothing to drive: the reserve alone
    assert settings_target(70.0, LIVE) == pytest.approx(90.0)  # 100 would be needed: the maximum caps it
    assert settings_target(95.0, LIVE) == pytest.approx(95.0)  # a drive above the cap is still covered
    assert settings_target(120.0, LIVE) == pytest.approx(100.0)


# --------------------------------------------------------------------------- comparison


def test_a_higher_reserve_buys_more_energy_and_costs_more() -> None:
    result = replay_settings(three_days(), LIVE, CjpSettings(reserve=50.0, maximum=90.0))
    assert result.status == "ok" and result.comparable
    assert result.alternative.grid_kwh > result.baseline.grid_kwh
    assert result.difference["cost"] > 0.0
    assert result.max_conservation_error_kwh < 1e-6
    # the car ends higher, which is valued explicitly and shown next to the raw cash difference
    assert result.alternative.final_soc > result.baseline.final_soc
    assert result.difference["terminal_energy_kwh"] > 0.0
    assert result.difference["adjusted_cost"] < result.difference["cost"]


def test_the_extra_energy_of_a_higher_reserve_is_valued_at_the_price_it_was_bought_at() -> None:
    # cheap hours 02-04 of every day: the extra energy is bought there, not at the period average
    prices = {n: (0.2 if 2 <= n % 24 <= 4 else 1.0) for n in range(60)}
    result = replay_settings(three_days(prices), LIVE, CjpSettings(reserve=50.0, maximum=90.0))
    d = result.difference
    paid = result.alternative.cost / result.alternative.grid_kwh
    assert d["terminal_adjustment"] == pytest.approx(d["terminal_energy_kwh"] * (1.0 + LOSS) * paid)
    # at the period average the cheap purchase would look like a gain: that is not what is claimed
    assert paid < result.average_price
    assert d["adjusted_cost"] > d["cost"] - d["terminal_energy_kwh"] * (1.0 + LOSS) * result.average_price
    # a pure level shift bought in the cheap hours is about neutral, not a large saving
    assert abs(d["adjusted_cost"]) < 0.25 * abs(d["terminal_energy_kwh"] * (1.0 + LOSS) * result.average_price)


def test_a_lower_reserve_is_cheaper_and_leaves_the_car_lower() -> None:
    result = replay_settings(three_days(), LIVE, CjpSettings(reserve=10.0, maximum=90.0))
    assert result.comparable
    assert result.difference["cost"] < 0.0
    assert result.difference["terminal_energy_kwh"] < 0.0
    assert result.alternative.lowest_soc < result.baseline.lowest_soc


def test_the_reserve_has_no_effect_when_the_maximum_already_decides_the_level() -> None:
    # a 70 % drive plus any reserve of 20 % or more is capped at the maximum of 80 % in both settings
    data = make(
        [(0, 12), (20, 32), (44, 56)], flat(60), events=[drive(14, 70.0), drive(36, 70.0)], start=20.0,
    )
    result = replay_settings(data, CjpSettings(30.0, 80.0), CjpSettings(40.0, 80.0))
    assert result.comparable
    assert result.targets["baseline"] == result.targets["alternative"] == [80.0, 80.0]
    assert result.difference["cost"] == pytest.approx(0.0)
    assert result.difference["grid_kwh"] == pytest.approx(0.0)


def test_both_settings_are_replayed_by_the_same_charging_physics_and_prices() -> None:
    result = replay_settings(three_days({**flat(60), 3: 0.1, 4: 0.1, 5: 0.1, 6: 0.1}), LIVE, CjpSettings(30.0, 70.0))
    assert result.status == "ok"
    # the energy bought in a window is what the target needs: grid = battery * (1 + loss)
    assert result.baseline.grid_kwh == pytest.approx(result.baseline.charged_kwh * (1.0 + LOSS))
    assert result.alternative.grid_kwh == pytest.approx(result.alternative.charged_kwh * (1.0 + LOSS))


def test_the_last_connection_is_left_out_because_its_next_drive_is_unknown() -> None:
    result = replay_settings(three_days(), LIVE, CjpSettings(50.0, 90.0))
    assert result.windows_total == 3 and result.windows_replayed == 2
    assert len(result.baseline.windows) == 2 and len(result.targets["baseline"]) == 2
    assert "last_connection_left_out" in result.notes


def test_a_single_connection_is_not_enough() -> None:
    result = replay_settings(make([(0, 8)], flat(10), start=50.0), LIVE, CjpSettings(50.0, 90.0))
    assert result.status == "unavailable" and result.reason == "too_few_windows"


def test_the_two_settings_are_judged_against_one_reserve() -> None:
    # the baseline reserve (30) is the yardstick for both: the lower reserve arrives below it
    data = make([(0, 8), (20, 28), (40, 48)], flat(50), events=[drive(12, 20.0), drive(30, 20.0)], start=40.0)
    result = replay_settings(data, LIVE, CjpSettings(reserve=10.0, maximum=90.0))
    assert result.baseline_readiness.arrivals_below_reserve == 0
    assert result.alternative_readiness.arrivals_below_reserve == 2
    assert result.alternative_readiness.lowest_arrival == pytest.approx(10.0)  # also the level after the last drive


# --------------------------------------------------------------------------- honesty gates


def test_a_setting_that_covers_less_of_the_driving_does_not_look_cheaper() -> None:
    # connection 2 is only an hour long: the car must arrive with enough from the long connection 1
    data = make(
        [(0, 8), (20, 21), (40, 48)], flat(50), events=[drive(12, 30.0), drive(30, 40.0)], start=10.0,
    )
    result = replay_settings(data, LIVE, CjpSettings(reserve=0.0, maximum=90.0))
    assert result.status == "ok"
    assert result.alternative.unmet_driving_kwh > result.baseline.unmet_driving_kwh
    assert not result.comparable and result.comparable_reason == "unmet_driving_differs"
    assert result.difference is None


def test_identical_settings_are_not_a_comparison() -> None:
    result = replay_settings(three_days(), LIVE, CjpSettings(30.0, 90.0))
    assert result.status == "unavailable" and result.reason == "no_change"


@pytest.mark.parametrize(
    "baseline, alternative",
    [
        (CjpSettings(30.0, 90.0), CjpSettings(-1.0, 90.0)),
        (CjpSettings(30.0, 90.0), CjpSettings(30.0, 0.0)),
        (CjpSettings(30.0, 90.0), CjpSettings(30.0, 120.0)),
        (CjpSettings(math.nan, 90.0), CjpSettings(30.0, 90.0)),
        (CjpSettings(30.0, 90.0), CjpSettings(30.0, math.inf)),
    ],
)
def test_settings_out_of_range_are_not_replayed(baseline, alternative) -> None:
    result = replay_settings(three_days(), baseline, alternative)
    assert result.status == "unavailable" and result.reason == "invalid_settings"


def test_an_unknown_start_level_brings_both_settings_to_the_same_level() -> None:
    result = replay_settings(replace(three_days(), start_soc=None), LIVE, CjpSettings(50.0, 90.0))
    assert result.status == "ok" and result.calibrated_start and "start_level_unknown_calibrated" in result.warnings
    first_base, first_alt = result.baseline.windows[0], result.alternative.windows[0]
    # the first connection is only used to bring the car to one common level: nothing is charged or counted in it
    assert first_base.calibrated and first_alt.calibrated and first_base.cost == first_alt.cost == 0.0
    assert first_base.soc_out == first_alt.soc_out == pytest.approx(result.targets["baseline"][0])
    assert result.comparable and result.max_conservation_error_kwh < 1e-6


def test_no_connections_is_reported_before_the_settings() -> None:
    result = replay_settings(make([], flat(2), start=50.0), LIVE, CjpSettings(30.0, 90.0))
    assert result.status == "unavailable" and result.reason == "no_windows"


def test_a_connection_without_prices_is_priced_at_one_flat_price_for_both_settings() -> None:
    prices = {i: 1.0 + (i % 5) * 0.2 for i in range(60)}
    for i in range(20, 32):
        del prices[i]  # connection 2 has no price
    result = replay_settings(three_days(prices), LIVE, CjpSettings(reserve=50.0, maximum=90.0))
    assert result.status == "ok" and result.method == "estimated" and result.estimated_windows == 1
    assert result.windows_replayed == 2 and "prices_estimated_windows" in result.warnings
    base, alt = result.baseline.windows[1], result.alternative.windows[1]
    assert base.estimated and alt.estimated
    # both settings pay the same estimated price per kWh: the one that charges more does not get its energy for free
    assert base.cost == pytest.approx(base.grid_kwh * result.price_estimate)
    assert alt.cost == pytest.approx(alt.grid_kwh * result.price_estimate)
    assert base.cost / base.grid_kwh == pytest.approx(alt.cost / alt.grid_kwh)  # one price per kWh for both settings
    assert "measured_cost" in result.difference


def test_fewer_than_half_priced_connections_give_no_settings_result() -> None:
    prices = {i: 1.0 for i in range(8)}  # only connection 1 of the two compared ones
    prices_none = {}
    assert replay_settings(three_days(prices_none or {99: 1.0}), LIVE, CjpSettings(50.0, 90.0)).reason == "missing_prices"
    assert replay_settings(three_days(prices), LIVE, CjpSettings(50.0, 90.0)).status == "ok"  # one of two is enough


def test_a_model_that_returns_nothing_usable_is_unavailable() -> None:
    result = replay_settings(three_days(loss=lambda kw: math.nan), LIVE, CjpSettings(50.0, 90.0))
    assert result.status == "unavailable" and result.reason == "invalid_loss_model"


def test_the_settings_replay_does_not_change_scenario_a() -> None:
    data = three_days(target=80.0)
    before = replay(data)
    replay_settings(data, LIVE, CjpSettings(50.0, 90.0))
    after = replay(data)
    assert (before.smart.cost, before.asap.cost, before.status) == (after.smart.cost, after.asap.cost, after.status)
