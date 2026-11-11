"""What-if replay: the pure smart-to-target replay against an immediate-charging reference."""
from __future__ import annotations

import math

import pytest

from custom_components.cable_juice_planner.engine.whatif.replay import (
    Event,
    ReplayInput,
    Window,
    replay,
)

H = 3600.0
T0 = 1_700_000_000 - 1_700_000_000 % 3600  # an exact UTC hour
BATTERY = 60.0  # kWh
KW = 10.0
LOSS = 0.1  # 10 % more from the charger than into the battery


def hour(n: float) -> float:
    return T0 + n * H


def make(windows, prices, *, events=(), start=20.0, target=80.0, power_cap=None, loss=lambda kw: LOSS, **kwargs) -> ReplayInput:
    extra = {"power_cap": power_cap} if power_cap else {}
    return ReplayInput(
        windows=tuple(Window(hour(a), hour(b)) for a, b in windows),
        events=tuple(events),
        prices={hour(n): p for n, p in prices.items()},
        battery_kwh=kwargs.get("battery", BATTERY),
        max_kw=KW,
        phases=3,
        start_soc=start,
        target_soc=target,
        loss_factor=loss,
        **extra,
    )


def flat(n: int, price: float = 1.0) -> dict[int, float]:
    return {i: price for i in range(n)}


def test_identical_when_there_is_only_one_possible_plan() -> None:
    # 20 -> 80 % = 36 kWh into the battery; one hour at 10 kW cannot do it: both use the whole hour
    result = replay(make([(0, 1)], {0: 1.0}))
    assert result.status == "ok"
    assert result.smart.cost == pytest.approx(result.asap.cost)
    assert result.difference["cost"] == pytest.approx(0.0)
    assert result.difference["adjusted_cost"] == pytest.approx(0.0)
    assert result.smart.final_soc == pytest.approx(result.asap.final_soc)


def test_smart_uses_the_cheapest_hours_and_both_reach_the_same_target() -> None:
    # needs 36 kWh = 4 h at 10 kW / 1.1 = 9.09 kWh per hour; prices fall in the window
    prices = {0: 3.0, 1: 3.0, 2: 3.0, 3: 3.0, 4: 1.0, 5: 1.0, 6: 1.0, 7: 1.0, 8: 2.0}
    result = replay(make([(0, 9)], prices))
    assert result.status == "ok"
    assert result.smart.final_soc == pytest.approx(80.0) and result.asap.final_soc == pytest.approx(80.0)
    assert result.asap.cost > result.smart.cost
    assert result.difference["cost"] < 0.0
    assert result.difference["terminal_energy_kwh"] == pytest.approx(0.0)
    assert result.difference["adjusted_cost"] == pytest.approx(result.difference["cost"])
    # the energy that was charged is the same, and the loss is applied on the charger side
    assert result.smart.charged_kwh == pytest.approx(36.0)
    assert result.smart.grid_kwh == pytest.approx(36.0 * 1.1)
    assert result.smart.loss_kwh == pytest.approx(3.6)
    assert result.max_conservation_error_kwh < 1e-6


def test_negative_prices_are_used_and_not_floored() -> None:
    prices = {0: 2.0, 1: -1.0, 2: -1.0, 3: -1.0, 4: -1.0, 5: 2.0}
    result = replay(make([(0, 6)], prices))
    assert result.smart.cost < 0.0
    assert result.smart.cost < result.asap.cost


def test_soc_is_carried_between_windows_and_drives_withdraw_it() -> None:
    drive = Event(ts=hour(10), kind="drive", soc_percent=30.0)
    result = replay(make([(0, 6), (20, 26)], flat(26), events=[drive], target=80.0, start=80.0))
    first, second = result.smart.windows
    assert first.soc_out == pytest.approx(80.0)
    assert second.soc_in == pytest.approx(50.0)  # not reset to the start SOC
    assert second.soc_out == pytest.approx(80.0)
    assert result.smart.lowest_soc == pytest.approx(50.0)
    assert result.smart.driven_percent == pytest.approx(30.0)
    assert result.max_conservation_error_kwh < 1e-6


def test_unmet_target_is_reported_and_buying_less_is_not_a_win() -> None:
    # a 2 hour window cannot reach 80 % from 20 %; both policies fall short by the same amount
    result = replay(make([(0, 2)], {0: 1.0, 1: 5.0}))
    assert result.smart.unmet_target_kwh == pytest.approx(result.asap.unmet_target_kwh)
    assert result.smart.unmet_target_kwh > 0.0
    assert result.smart.targets_met == 0
    # more energy is delivered when more hours are used: the infeasible policy cannot hide behind a lower cost
    assert result.smart.charged_kwh == pytest.approx(result.asap.charged_kwh)


def test_driving_shortfall_is_unmet_demand_and_the_soc_stops_at_zero() -> None:
    drive = Event(ts=hour(6), kind="drive", soc_percent=50.0)
    result = replay(make([(0, 1), (10, 11)], flat(11), events=[drive], target=30.0, start=10.0))
    # one hour gives 10 kW / 1.1 = 9.09 kWh = 15.15 % (10 -> 25.15 %); then 50 % is needed but only 25.15 % is there
    reached = 10.0 + (KW / (1 + LOSS)) / BATTERY * 100.0
    assert result.smart.windows[0].soc_out == pytest.approx(reached)
    assert result.smart.unmet_driving_kwh == pytest.approx((50.0 - reached) / 100.0 * BATTERY)
    assert result.smart.lowest_soc == pytest.approx(0.0)
    assert result.max_conservation_error_kwh < 1e-6


def test_a_connection_without_prices_is_estimated_at_the_average_of_the_priced_hours() -> None:
    prices = {**{i: 1.0 for i in range(6)}, **{i: 3.0 for i in range(40, 46)}}  # the middle connection (20-26) has no price at all
    result = replay(make([(0, 6), (20, 26), (40, 46)], prices))
    assert result.status == "ok" and result.method == "estimated" and result.estimated_windows == 1
    assert result.price_estimate == pytest.approx(2.0)  # the time-weighted average of the priced hours, never a favourable value
    assert result.windows_replayed == 3 and "prices_estimated_windows" in result.warnings
    smart_row, asap_row = result.smart.windows[1], result.asap.windows[1]
    assert smart_row.estimated and asap_row.estimated and not result.smart.windows[0].estimated
    # at one flat price smart has nothing to choose between: no policy gets cheaper energy from an estimate
    assert smart_row.cost == pytest.approx(asap_row.cost)
    assert smart_row.cost == pytest.approx(smart_row.grid_kwh * 2.0)
    # the measured part is shown next to the total
    assert result.difference["measured_cost"] == pytest.approx(result.difference["cost"])
    assert result.average_price == pytest.approx(2.0)


def test_a_single_missing_hour_is_estimated_and_the_rest_of_the_connection_is_measured() -> None:
    prices = flat(30)
    del prices[22]
    result = replay(make([(0, 6), (20, 26), (40, 46)], {**prices, **{i: 1.0 for i in range(40, 46)}}))
    assert result.status == "ok" and result.estimated_windows == 1 and result.windows_replayed == 3


def test_fewer_than_half_of_the_connections_with_prices_gives_no_result() -> None:
    prices = {i: 1.0 for i in range(6)}  # only the first of three connections is priced
    result = replay(make([(0, 6), (20, 26), (40, 46)], prices))
    assert result.status == "unavailable" and result.reason == "missing_prices"
    assert replay(make([(0, 6)], {})).reason == "missing_prices"  # nothing priced at all: nothing to estimate from
    half = replay(make([(0, 6), (20, 26)], prices))  # exactly half is enough
    assert half.status == "ok" and half.method == "estimated"


def test_an_unknown_start_level_makes_the_first_connection_bring_the_car_to_the_target() -> None:
    drive = Event(ts=hour(10), kind="drive", soc_percent=30.0)
    result = replay(make([(0, 6), (20, 26)], flat(30), events=[drive], start=None, target=80.0))
    assert result.status == "ok" and result.method == "estimated" and result.calibrated_start
    assert "start_level_unknown_calibrated" in result.warnings
    first = result.smart.windows[0]
    assert first.calibrated and first.cost == 0.0 and first.charged_kwh == 0.0 and first.soc_out == pytest.approx(80.0)
    assert result.smart.windows[1].soc_in == pytest.approx(50.0)  # the drive is applied from the common level
    assert result.smart.windows[1].soc_out == pytest.approx(80.0) and result.max_conservation_error_kwh < 1e-6
    assert result.comparable and result.difference is not None


def test_non_finite_price_counts_as_missing() -> None:
    result = replay(make([(0, 3)], {0: 1.0, 1: math.nan, 2: 1.0}))
    assert result.status == "unavailable" and result.reason == "missing_prices"
    result = replay(make([(0, 3)], {0: 1.0, 1: math.inf, 2: 1.0}))
    assert result.status == "unavailable"


def test_public_charge_that_would_overfill_stops_the_replay() -> None:
    public = Event(ts=hour(10), kind="public", battery_kwh=20.0)  # 33 % on a 60 kWh battery
    result = replay(make([(0, 6), (20, 26)], flat(26), events=[public], target=80.0, start=80.0))
    assert result.status == "partial" and result.reason == "public_overfill"
    assert result.windows_replayed == 1
    # a fixed public charge that fits is carried like any other energy
    ok = replay(make([(0, 6), (20, 26)], flat(26), events=[Event(ts=hour(10), kind="public", battery_kwh=6.0)], target=80.0, start=60.0))
    assert ok.status == "ok" and ok.smart.public_kwh == pytest.approx(6.0)
    # 60 % -> target 80 % in the first window, then 6 kWh = 10 % from the public charger: 90 % at the next plug-in
    assert ok.smart.windows[0].soc_out == pytest.approx(80.0)
    assert ok.smart.windows[1].soc_in == pytest.approx(90.0)
    assert ok.smart.windows[1].charged_kwh == 0.0
    assert ok.max_conservation_error_kwh < 1e-6


def test_soc_dependent_power_limits_the_charge() -> None:
    def taper(phases: int, soc_start: float, soc_end: float, base_kw: float) -> float:
        return min(base_kw, 5.0) if soc_end > 60.0 else base_kw

    free = replay(make([(0, 4)], flat(4)))
    tapered = replay(make([(0, 4)], flat(4), power_cap=taper))
    assert tapered.smart.charged_kwh < free.smart.charged_kwh
    assert tapered.smart.unmet_target_kwh > free.smart.unmet_target_kwh
    # smart can never deliver less than charging immediately: it uses every hour if it must
    assert tapered.smart.charged_kwh == pytest.approx(tapered.asap.charged_kwh)
    assert tapered.comparable and tapered.difference["cost"] == pytest.approx(0.0)


def test_partial_hours_use_the_fraction_of_the_window() -> None:
    # connected from half past to half past: two half hours and one full hour
    data = ReplayInput(
        windows=(Window(hour(0.5), hour(2.5)),), events=(), prices={hour(0): 1.0, hour(1): 1.0, hour(2): 1.0},
        battery_kwh=BATTERY, max_kw=KW, phases=3, start_soc=0.0, target_soc=100.0, loss_factor=lambda kw: 0.0,
    )
    result = replay(data)
    assert result.asap.grid_kwh == pytest.approx(20.0)  # 2 hours at 10 kW, not 3
    assert result.smart.grid_kwh == pytest.approx(20.0)


def test_a_25_hour_local_day_is_25_distinct_utc_hours() -> None:
    result = replay(make([(0, 25)], flat(25), start=0.0, target=100.0))
    assert result.asap.grid_kwh == pytest.approx(BATTERY * 1.1) and result.status == "ok"


def test_target_already_met_charges_nothing() -> None:
    result = replay(make([(0, 4)], flat(4), start=90.0, target=80.0))
    assert result.smart.charged_kwh == 0.0 and result.smart.cost == 0.0
    assert result.smart.targets_met == 1


def test_terminal_adjustment_is_explicit_and_consistent() -> None:
    result = replay(make([(0, 2)], {0: 1.0, 1: 1.0}))
    diff = result.difference
    assert diff["terminal_energy_kwh"] == pytest.approx(0.0)  # comparable policies end at the same energy
    assert diff["terminal_adjustment"] == pytest.approx(diff["terminal_energy_kwh"] * 1.1 * 1.0)
    assert diff["adjusted_cost"] == pytest.approx(diff["cost"] - diff["terminal_adjustment"])
    assert result.average_price == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"start": math.nan}, "no_start_soc"),
        ({"start": 120.0}, "no_start_soc"),
        ({"target": 0.0}, "invalid_target"),
        ({"battery": 0.0}, "no_battery_capacity"),
    ],
)
def test_invalid_inputs_are_unavailable_with_a_reason(kwargs, reason) -> None:
    result = replay(make([(0, 2)], flat(2), **kwargs))
    assert result.status == "unavailable" and result.reason == reason
    assert result.smart is None and result.difference is None


def test_windows_must_exist_and_not_overlap() -> None:
    assert replay(make([], flat(2))).reason == "no_windows"
    assert replay(make([(0, 3), (2, 5)], flat(6))).reason == "overlapping_windows"
    assert replay(make([(3, 3)], flat(6))).reason == "invalid_window"


def test_events_inside_a_connection_window_are_counted_not_replayed() -> None:
    inside = Event(ts=hour(2), kind="drive", soc_percent=10.0)
    result = replay(make([(0, 4), (10, 12)], flat(12), events=[inside]))
    assert result.ignored_events == 1
    assert result.smart.driven_percent == 0.0


def test_replay_is_deterministic_and_does_not_mutate_its_input() -> None:
    data = make([(0, 6), (20, 26)], {i: 1.0 + (i % 5) * 0.1 for i in range(26)}, events=[Event(hour(10), "drive", 20.0)])
    before = dict(data.prices)
    first, second = replay(data), replay(data)
    assert first.smart.cost == second.smart.cost and first.difference == second.difference
    assert dict(data.prices) == before


def test_a_blocked_soc_range_cannot_make_smart_look_cheaper() -> None:
    """The review scenario: the car accepts no power above 90 %. Smart must not win by charging nothing."""
    def blocked(phases: int, soc_start: float, soc_end: float, base_kw: float) -> float:
        return 0.0 if soc_end > 90.0 else base_kw

    prices = {i: 3.0 if i < 4 else 1.0 for i in range(8)}
    result = replay(make([(0, 8)], prices, start=20.0, target=100.0, power_cap=blocked))
    assert result.smart.charged_kwh == pytest.approx(result.asap.charged_kwh)
    assert result.smart.unmet_target_kwh == pytest.approx(result.asap.unmet_target_kwh)
    assert result.smart.unmet_target_kwh > 0.0
    assert result.smart.cost == pytest.approx(result.asap.cost)
    assert result.difference["cost"] == pytest.approx(0.0)


def test_policies_that_deliver_differently_are_not_comparable() -> None:
    """Defence in depth: if the unmet energy ever differs, no difference (and no winner) is published."""
    from custom_components.cable_juice_planner.engine.whatif import replay as module

    data = make([(0, 6)], flat(6))
    original = module._plan_smart
    try:
        module._plan_smart = lambda *args: {}  # a broken policy that charges nothing
        result = module.replay(data)
    finally:
        module._plan_smart = original
    assert result.status == "ok" and result.comparable is False
    assert result.comparable_reason == "unmet_target_differs"
    assert result.difference is None


def test_smart_picks_cheap_hours_with_the_real_taper_not_a_conservative_cap() -> None:
    def taper(phases: int, soc_start: float, soc_end: float, base_kw: float) -> float:
        return min(base_kw, 5.0) if soc_end > 70.0 else base_kw

    prices = {i: 1.5 if i < 4 else 1.0 for i in range(10)}
    result = replay(make([(0, 10)], prices, start=20.0, target=80.0, power_cap=taper))
    assert result.smart.final_soc == pytest.approx(80.0)
    # everything is charged in the cheap hours: the expensive early hours are not used
    assert result.smart.cost == pytest.approx(result.smart.grid_kwh * 1.0)
    assert result.asap.cost > result.smart.cost
    assert result.max_conservation_error_kwh < 1e-6


@pytest.mark.parametrize(
    ("callable_kwargs", "reason"),
    [
        ({"loss": lambda kw: math.nan}, "invalid_loss_model"),
        ({"loss": lambda kw: -0.5}, "invalid_loss_model"),
        ({"power_cap": lambda p, a, b, kw: math.nan}, "invalid_power_model"),
        ({"power_cap": lambda p, a, b, kw: -1.0}, "invalid_power_model"),
    ],
)
def test_invalid_models_are_unavailable_not_ok(callable_kwargs, reason) -> None:
    result = replay(make([(0, 4)], flat(4), **callable_kwargs))
    assert result.status == "unavailable" and result.reason == reason
    assert result.difference is None and result.smart is None


def test_sub_hour_price_keys_are_rejected_instead_of_priced_at_the_hour() -> None:
    data = make([(0, 2)], flat(2))
    prices = dict(data.prices)
    prices[hour(0) + 900.0] = 5.0
    quarter = ReplayInput(**{**data.__dict__, "prices": prices})
    assert replay(quarter).reason == "prices_not_hourly"


def test_events_before_the_first_window_and_after_the_last_are_counted() -> None:
    before = Event(ts=hour(-5), kind="drive", soc_percent=50.0)
    after = Event(ts=hour(30), kind="drive", soc_percent=10.0)
    result = replay(make([(0, 4), (10, 12)], flat(12), events=[before, after]))
    assert result.ignored_events == 2
    assert result.smart.driven_percent == 0.0 and result.warnings == ()


def test_a_drive_dated_inside_a_window_is_flagged() -> None:
    result = replay(make([(0, 4), (10, 12)], flat(12), events=[Event(hour(2), "drive", 10.0)]))
    assert result.warnings == ("events_inside_windows",)


def test_result_is_labelled_as_a_hindsight_replay_and_keeps_provenance() -> None:
    data = ReplayInput(**{**make([(0, 2)], flat(2)).__dict__, "provenance": {"prices": "measured", "soc": "reconstructed"}})
    result = replay(data)
    assert "hindsight" in result.notes and "solar_not_modelled" in result.notes
    assert result.provenance == {"prices": "measured", "soc": "reconstructed"}
    assert result.model_version >= 2


def test_average_price_is_time_weighted() -> None:
    # half an hour at 1.0 and a full hour at 4.0: (0.5*1 + 1*4) / 1.5
    data = ReplayInput(
        windows=(Window(hour(0.5), hour(2)),), events=(), prices={hour(0): 1.0, hour(1): 4.0},
        battery_kwh=BATTERY, max_kw=KW, phases=3, start_soc=0.0, target_soc=100.0, loss_factor=lambda kw: 0.0,
    )
    assert replay(data).average_price == pytest.approx(4.5 / 1.5)


def test_smart_uses_the_dearer_hour_only_for_what_the_cheap_hours_cannot_cover() -> None:
    """Prices 2, 1, 1, 1 and 36 kWh to charge at 10 kW: 30 kWh at 1, only the last 6 kWh at 2."""
    result = replay(make([(0, 4)], {0: 2.0, 1: 1.0, 2: 1.0, 3: 1.0}, loss=lambda kw: 0.0))
    assert result.smart.final_soc == pytest.approx(80.0)
    assert result.smart.cost == pytest.approx(30.0 * 1.0 + 6.0 * 2.0)
    assert result.asap.cost == pytest.approx(10.0 * 2.0 + 10.0 + 10.0 + 6.0)
    assert result.difference["cost"] == pytest.approx(-4.0)
    assert result.max_conservation_error_kwh < 1e-6


def test_a_drive_recorded_shortly_before_the_unplug_still_counts() -> None:
    """The drive start and the charger's unplug differ by seconds to minutes."""
    early = Event(ts=hour(6) - 600.0, kind="drive", soc_percent=30.0)  # 10 minutes before the unplug
    result = replay(make([(0, 6), (20, 26)], flat(26), events=[early], start=80.0))
    assert result.smart.driven_percent == pytest.approx(30.0)
    assert result.smart.windows[1].soc_in == pytest.approx(50.0)
    assert result.warnings == ()
    late = Event(ts=hour(6) - 3600.0, kind="drive", soc_percent=30.0)  # an hour before: inside the window
    flagged = replay(make([(0, 6), (20, 26)], flat(26), events=[late], start=80.0))
    assert flagged.warnings == ("events_inside_windows",) and flagged.smart.driven_percent == 0.0


def test_adapter_warnings_reach_the_result() -> None:
    data = ReplayInput(**{**make([(0, 2)], flat(2)).__dict__, "warnings": ("prices_legacy_unverified",)})
    assert replay(data).warnings == ("prices_legacy_unverified",)


def test_a_drive_between_two_overlapping_gaps_is_applied_once() -> None:
    """A connection shorter than the gap tolerance makes two gaps overlap: the drive still counts once."""
    # A ends at hour 8, a drive starts five minutes before, B is only two minutes long, then C
    windows = (Window(hour(0), hour(8)), Window(hour(8) + 120.0, hour(8) + 240.0), Window(hour(20), hour(26)))
    data = ReplayInput(
        windows=windows, events=(Event(hour(8) - 300.0, "drive", 20.0),), prices={hour(i): 1.0 for i in range(30)},
        battery_kwh=BATTERY, max_kw=KW, phases=3, start_soc=80.0, target_soc=80.0, loss_factor=lambda kw: 0.0,
    )
    result = replay(data)
    assert result.smart.driven_percent == pytest.approx(20.0)  # not 40
    assert result.max_conservation_error_kwh < 1e-6


def test_no_connections_is_reported_before_a_missing_start_level() -> None:
    assert replay(make([], flat(2), start=None)).reason == "no_windows"
    # an unknown level is calibrated, not defaulted: a single connection would only calibrate and replay nothing
    assert replay(make([(0, 2)], flat(2), start=None)).reason == "too_few_windows"
    assert replay(make([(0, 2), (10, 12)], flat(14), start=None)).calibrated_start


def test_one_missing_hour_makes_the_whole_connection_estimated_so_smart_gets_no_cheap_hour_from_it() -> None:
    # the connection 0-8 has a cheap hour 2 and an expensive hour 5, hour 6 has no price: smart must not use an estimate to look cheaper
    prices = {0: 2.0, 1: 2.0, 2: 0.1, 3: 2.0, 4: 2.0, 5: 5.0, 7: 2.0}
    result = replay(make([(0, 8), (20, 22)], {**prices, 20: 1.0, 21: 1.0}))
    assert result.status == "ok" and result.estimated_windows == 1
    first_smart, first_asap = result.smart.windows[0], result.asap.windows[0]
    assert first_smart.estimated and first_smart.cost == pytest.approx(first_asap.cost)  # one flat price: nothing to choose between
    assert result.difference["measured_cost"] == pytest.approx(result.difference["cost"])


def test_the_calibrating_connection_is_not_counted_as_estimated_even_without_prices() -> None:
    drive = Event(ts=hour(10), kind="drive", soc_percent=30.0)
    prices = {i: 1.0 for i in range(20, 26)}  # the first connection (0-6) has no price, the second does
    result = replay(make([(0, 6), (20, 26)], prices, events=[drive], start=None))
    assert result.status == "ok" and result.calibrated_start and result.estimated_windows == 0 and result.method == "estimated"
