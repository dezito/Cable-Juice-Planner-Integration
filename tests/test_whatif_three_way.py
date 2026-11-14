"""What-if scenario A: CJP's own recorded charging against "smart" and "asap" (pure replay, no Home Assistant)."""
from __future__ import annotations

import pytest

from custom_components.cable_juice_planner.engine.whatif.replay import (
    RECORDED_NOTES,
    Event,
    Recorded,
    ReplayInput,
    Window,
    merge_results,
    replay,
    replay_segments,
)

H = 3600.0
T0 = 1_700_000_000 - 1_700_000_000 % 3600  # an exact UTC hour
BATTERY = 60.0  # kWh
KW = 10.0
LOSS = 0.1  # 10 % more from the charger than into the battery


def hour(n: float) -> float:
    return T0 + n * H


def flat(n: int, price: float = 1.0) -> dict[int, float]:
    return {i: price for i in range(n)}


def make(windows, prices, recorded, *, events=(), start=20.0, target=80.0, end_soc=None, deadlines=None, reserve=None, **kwargs) -> ReplayInput:
    deadlines = deadlines or {}
    return ReplayInput(
        windows=tuple(Window(hour(a), hour(b), deadline=hour(deadlines[a]) if a in deadlines else None) for a, b in windows),
        events=tuple(events),
        prices={hour(n): p for n, p in prices.items()},
        battery_kwh=BATTERY,
        max_kw=KW,
        phases=3,
        start_soc=start,
        target_soc=target,
        loss_factor=lambda kw: LOSS,
        recorded={hour(n): value for n, value in recorded.items()},
        end_soc=end_soc,
        arrival_reserve=reserve,
        **kwargs,
    )


def charged(percent: float, price: float = 1.0, local: float = 0.0) -> Recorded:
    """Recorded charging that raised the battery by `percent`: the charger gave percent / 100 * 60 * 1.1 kWh."""
    kwh = percent / 100.0 * BATTERY * (1.0 + LOSS)
    return Recorded(kwh=kwh, percent=percent, cost=kwh * price, local_kwh=local)


def test_a_record_that_charged_at_once_costs_what_charging_at_once_costs() -> None:
    result = replay(make([(0, 6)], flat(6), {0: charged(60.0)}, end_soc=80.0))
    assert result.status == "ok" and result.notes == RECORDED_NOTES and result.cjp is not None
    assert result.cjp.cost == pytest.approx(result.asap.cost) and result.cjp.grid_kwh == pytest.approx(result.asap.grid_kwh)
    assert result.cjp.final_soc == pytest.approx(80.0) and result.asap.final_soc == pytest.approx(80.0)
    assert result.differences["cjp_asap"]["cost"] == pytest.approx(0.0) and result.differences["cjp_asap"]["adjusted_cost"] == pytest.approx(0.0)


PRICES = {0: 3.0, 1: 3.0, 2: 3.0, 3: 3.0, 4: 1.0, 5: 1.0, 6: 1.0, 7: 1.0, 8: 2.0}


def test_a_record_charged_in_the_cheapest_hours_costs_what_smart_costs() -> None:
    result = replay(make([(0, 9)], PRICES, {0: charged(60.0, price=1.0)}, end_soc=80.0))
    assert result.cjp.cost == pytest.approx(result.smart.cost) and result.smart.cost < result.asap.cost
    assert result.differences["cjp_smart"]["cost"] == pytest.approx(0.0) and result.differences["cjp_asap"]["cost"] < 0.0


def test_smart_charges_before_the_planned_departure_not_before_the_real_unplug() -> None:
    free = replay(make([(0, 9)], PRICES, {0: charged(60.0)}))
    capped = replay(make([(0, 9)], PRICES, {0: charged(60.0)}, deadlines={0: 6}))  # told 06:00: hours 6-8 are not available
    assert free.smart.cost == pytest.approx(39.6) and capped.smart.cost > 70.0  # only two cheap hours are left, the rest is dear
    assert capped.smart.final_soc == pytest.approx(80.0)
    # a departure before the plug-in, or none, changes nothing
    assert replay(make([(0, 9)], PRICES, {0: charged(60.0)}, deadlines={0: 0})).smart.cost == pytest.approx(39.6)


def test_the_energy_cjp_bought_that_no_drive_explains_is_driving_in_all_three() -> None:
    # 50 % + 30 = 80, a 20 % drive, + 30 = 70 + 20 = 90 by the record: but the car has 70 %: 20 % went on small trips and standby
    drive = Event(hour(4.5), "drive", 20.0)
    windows = [(0, 4), (6, 10)]
    result = replay(make(windows, flat(10), {0: charged(30.0), 6: charged(30.0)}, events=[drive], start=50.0, end_soc=70.0))
    assert result.status == "ok" and result.energy["gap_percent"] == pytest.approx(20.0)
    assert result.cjp.final_soc == pytest.approx(70.0)  # it ends where the car was measured to be
    # every calculation was driven the same: the recorded drive and the gap
    assert result.cjp.driven_percent == pytest.approx(40.0)
    assert result.asap.driven_percent == pytest.approx(40.0) and result.smart.driven_percent == pytest.approx(40.0)
    assert result.differences["cjp_asap"]["adjusted_cost"] is not None and "energy_not_balanced" not in result.warnings


def test_without_the_end_level_the_three_are_not_known_to_be_balanced_and_no_adjusted_difference_is_given() -> None:
    result = replay(make([(0, 4), (6, 10)], flat(10), {0: charged(30.0), 6: charged(30.0)}, start=50.0))
    assert "energy_not_balanced" in result.warnings and result.energy["gap_percent"] is None
    assert result.comparable and result.differences["cjp_asap"]["cost"] is not None
    for key in ("cjp_asap", "cjp_smart", "smart_asap"):
        assert result.differences[key]["adjusted_cost"] is None and result.differences[key]["terminal_adjustment"] is None


def test_charging_cjp_did_not_record_is_said_and_not_turned_into_negative_driving() -> None:
    # the car holds 95 % where the record and the drives give 60 %: nothing in the record explains it
    result = replay(make([(0, 4), (6, 10)], flat(10), {0: charged(10.0), 6: charged(0.0)}, start=50.0, end_soc=95.0))
    assert "recorded_energy_short" in result.warnings and result.energy["gap_percent"] < 0
    assert result.asap.driven_percent == pytest.approx(0.0)


def test_a_record_that_charges_past_full_is_said() -> None:
    result = replay(make([(0, 4)], flat(4), {0: charged(70.0)}, start=50.0))
    assert "recorded_path_cut" in result.warnings and result.cjp.final_soc == pytest.approx(100.0) and result.energy["cut_percent"] == pytest.approx(20.0)


def test_solar_and_home_battery_energy_is_priced_at_the_grid_price_and_the_share_is_said() -> None:
    mostly_local = Recorded(kwh=39.6, percent=60.0, cost=39.6, local_kwh=20.0)
    result = replay(make([(0, 6)], flat(6), {0: mostly_local}))
    assert "recorded_solar_share" in result.warnings and result.energy["local_kwh"] == pytest.approx(20.0)
    assert result.cjp.cost == pytest.approx(39.6)  # all of it at the hour's grid price: nothing is free
    clean = replay(make([(0, 6)], flat(6), {0: charged(60.0)}))
    assert "recorded_solar_share" not in clean.warnings


def test_the_arrival_level_is_judged_against_the_daily_minimum_for_cjp() -> None:
    drive = Event(hour(4.5), "drive", 40.0)
    result = replay(make([(0, 4), (6, 10)], flat(10), {0: charged(0.0), 6: charged(0.0)}, events=[drive], start=50.0, reserve=30.0))
    assert result.cjp.windows[0].unmet_target_kwh == pytest.approx(20.0 / 100.0 * BATTERY)  # home with 10 % under a 30 % minimum
    assert result.cjp.windows[1].unmet_target_kwh == 0.0  # the arrival after the last connection is not known


def test_the_three_are_only_compared_when_all_cover_the_driving() -> None:
    # CJP charged 50 % and covered the drive of 90 % (it was driven), but charging to the target (80 %) leaves the replayed
    # calculations 10 % short: they do not deliver what CJP did
    drive = Event(hour(4.5), "drive", 90.0)
    result = replay(make([(0, 4), (6, 10)], flat(10), {0: charged(50.0), 6: charged(0.0)}, events=[drive], start=50.0))
    assert result.cjp.unmet_driving_kwh == 0 and result.asap.unmet_driving_kwh > 0 and result.smart.unmet_driving_kwh > 0
    assert result.comparable is False and result.comparable_reason == "unmet_driving_differs" and result.differences is None


def test_a_shortfall_in_the_reconstructed_level_of_cjp_is_a_warning_and_not_an_uncovered_drive() -> None:
    # CJP's record leaves the car at 50 % and the drive uses 60 %: the record and the drive do not fit, but the car was driven
    # (so the path is wrong, not the car): said, and kept out of what is compared; the replayed calculations cover the drive
    drive = Event(hour(4.5), "drive", 60.0)
    result = replay(make([(0, 4), (6, 10)], flat(10), {0: charged(0.0), 6: charged(0.0)}, events=[drive], start=50.0))
    assert "recorded_path_short" in result.warnings and result.energy["path_shortfall_kwh"] > 0
    assert result.cjp.unmet_driving_kwh == 0 and result.asap.unmet_driving_kwh == 0 and result.comparable is True
    # the level of CJP is the path itself, not held at 0 % (50 - 60 = -10): its end level and its rows are the path's own
    assert result.cjp.final_soc == pytest.approx(-10.0, abs=0.01) and result.cjp.windows[-1].soc_out == pytest.approx(-10.0, abs=0.01)
    assert result.cjp.lowest_soc == pytest.approx(-10.0, abs=0.01) and result.energy["path_shortfall_kwh"] == pytest.approx(6.0, abs=0.01)


def test_the_record_must_cover_every_connection_and_the_start_level_must_be_known() -> None:
    assert replay(make([(0, 4), (6, 10)], flat(10), {0: charged(10.0)})).reason == "no_recorded_charging"
    assert replay(make([(0, 4), (6, 10)], flat(10), {0: charged(10.0), 6: charged(10.0)}, start=None)).reason in ("no_start_soc", "too_few_windows")


def test_the_price_per_kwh_of_each_and_cjps_kwh_at_the_price_of_the_others() -> None:
    # CJP and Smart buy in the cheap hours (1.0 per kWh); charging at once buys from the plug-in, in the dear hours (3.0)
    result = replay(make([(0, 9)], PRICES, {0: charged(60.0, price=1.0)}, end_soc=80.0))
    unit = result.unit_prices
    assert unit["cjp"] == pytest.approx(1.0) and unit["smart"] == pytest.approx(1.0) and unit["asap"] == pytest.approx(3.0)
    assert unit["cjp_kwh"] == pytest.approx(result.cjp.grid_kwh) and unit["cjp_cost"] == pytest.approx(result.cjp.cost)
    # what CJP's own kWh would have cost at the price per kWh of the others, and CJP's actual cost minus that
    assert unit["asap_at_cjp_kwh"] == pytest.approx(3.0 * result.cjp.grid_kwh) and unit["smart_at_cjp_kwh"] == pytest.approx(result.cjp.cost)
    assert unit["cjp_vs_asap"] == pytest.approx(result.cjp.cost - 3.0 * result.cjp.grid_kwh) and unit["cjp_vs_asap"] < 0
    assert unit["cjp_vs_smart"] == pytest.approx(0.0)


def test_the_price_per_kwh_is_given_when_the_three_cannot_be_compared_on_the_amount() -> None:
    drive = Event(hour(4.5), "drive", 90.0)  # more than the target leaves: the replayed calculations are short, CJP covered it
    result = replay(make([(0, 4), (6, 10)], flat(10), {0: charged(0.0), 6: charged(0.0)}, events=[drive], start=50.0))
    assert result.comparable is False and result.differences is None
    assert result.unit_prices is not None and result.unit_prices["asap"] == pytest.approx(1.0)  # the price does not depend on the amount
    assert result.unit_prices["cjp"] is None and result.unit_prices["cjp_vs_asap"] is None  # CJP bought nothing: it has no price


def test_cjp_is_never_cheaper_than_the_cheapest_hours_for_the_same_energy() -> None:
    """A cross-check of the pricing: a record priced hour by hour cannot beat the cheapest hours of its own connection."""
    result = replay(make([(0, 9)], PRICES, {0: charged(60.0, price=1.0)}))
    assert result.cjp.cost >= result.smart.cost - 1e-9


# --------------------------------------------------------------------------- stretches without a gap


def _stretch(first: int, price: float, **kwargs) -> ReplayInput:
    """Two connections of 9 hours that start at hour `first`, CJP charging 40 % in each of them, 40 % driven in the gap."""
    a, b = first, first + 10
    drive = Event(hour(a + 9.5), "drive", 40.0)
    prices = {n: price for n in range(first, first + 20)}
    return make([(a, a + 9), (b, b + 9)], prices, {a: charged(40.0, price), b: charged(40.0, price)}, events=[drive], end_soc=60.0, start=60.0, **kwargs)


def test_stretches_are_replayed_on_their_own_and_added_up() -> None:
    one, two = _stretch(0, 1.0), _stretch(100, 2.0)
    first, second = replay(one), replay(two)
    merged = replay_segments([one, two])
    assert merged.status == "ok" and merged.segments == 2 and merged.comparable is True and len(merged.parts) == 2
    for name in ("cjp", "smart", "asap"):
        policy, a, b = getattr(merged, name), getattr(first, name), getattr(second, name)
        assert policy.cost == pytest.approx(a.cost + b.cost) and policy.grid_kwh == pytest.approx(a.grid_kwh + b.grid_kwh)
        assert [w.segment for w in policy.windows] == [0, 0, 1, 1] and policy.finals == pytest.approx([a.final_soc, b.final_soc])
        assert policy.final_soc == pytest.approx(b.final_soc) and policy.start_soc == pytest.approx(a.start_soc)
    for key in ("cjp_asap", "cjp_smart", "smart_asap"):
        for field in ("cost", "adjusted_cost", "grid_kwh", "terminal_energy_kwh"):
            assert merged.differences[key][field] == pytest.approx(first.differences[key][field] + second.differences[key][field])
    assert merged.difference == merged.differences["cjp_asap"] and merged.windows_total == 4 and merged.windows_replayed == 4
    # the price of the period weights each stretch by its hours; the energy of the three is the sum
    assert merged.average_price == pytest.approx((1.0 * first.priced_hours + 2.0 * second.priced_hours) / (first.priced_hours + second.priced_hours))
    assert merged.energy["charged_percent"] == pytest.approx(first.energy["charged_percent"] + second.energy["charged_percent"])
    assert merged.unit_prices["cjp_kwh"] == pytest.approx(merged.cjp.grid_kwh)


def test_a_stretch_never_carries_its_level_over_a_gap() -> None:
    """The second stretch starts from its own level, not from where the first one ended."""
    low, high = _stretch(0, 1.0), _stretch(100, 1.0)
    from dataclasses import replace as _replace

    high = _replace(high, start_soc=30.0, end_soc=30.0)
    merged = replay_segments([low, high])
    assert merged.cjp.windows[2].soc_in == pytest.approx(30.0) and merged.cjp.windows[0].soc_in == pytest.approx(60.0)
    assert merged.smart.windows[2].soc_in == pytest.approx(30.0)


def test_a_stretch_of_one_connection_without_a_level_is_left_out_and_counted_not_the_whole_result() -> None:
    from dataclasses import replace as _replace

    alone = make([(200, 209)], {n: 1.0 for n in range(200, 209)}, {200: charged(20.0)}, start=None, end_soc=None)
    merged = replay_segments([_stretch(0, 1.0), _replace(alone, may_calibrate=True)])
    assert merged.status == "ok" and merged.segments == 1 and merged.skipped_segments == [(1, "too_few_windows")] and merged.windows_total == 2


def test_a_stretch_with_another_failure_makes_the_whole_result_unavailable_with_that_reason() -> None:
    first = _stretch(0, 1.0)
    prices_missing = _stretch(100, 1.0)
    from dataclasses import replace as _replace

    prices_missing = _replace(prices_missing, phases=5)  # a failure that is not about the stretch itself: the charger cannot be modelled
    merged = replay_segments([first, prices_missing])
    assert merged.status == "unavailable" and merged.reason == "no_charging_power"
    # the first stretch has no start level and may not calibrate: that is refused too, never made up
    refused = _replace(first, start_soc=None)
    assert replay_segments([refused, _stretch(100, 1.0)]).reason == "no_start_soc"
    assert replay_segments([]).reason == "no_windows" and merge_results([]).status == "unavailable"


def test_the_progress_is_counted_over_all_the_stretches() -> None:
    seen: list[tuple[int, int]] = []
    replay_segments([_stretch(0, 1.0), _stretch(100, 1.0)], lambda done, total: seen.append((done, total)))
    assert seen == [(0, 4), (1, 4), (2, 4), (3, 4)]


def test_a_stretch_of_one_connection_is_left_out_also_with_a_known_level_and_counted() -> None:
    from dataclasses import replace as _replace

    alone = make([(200, 209)], {n: 1.0 for n in range(200, 209)}, {200: charged(20.0)}, start=40.0, end_soc=50.0)
    merged = replay_segments([_stretch(0, 1.0), _replace(alone, may_calibrate=True)])
    assert merged.status == "ok" and merged.segments == 1 and merged.skipped_segments == [(1, "too_few_windows")]
    assert "recorded_stretches_left_out" in merged.warnings and merged.differences["cjp_asap"]["adjusted_cost"] is not None


def test_a_stretch_with_too_few_prices_is_left_out_and_does_not_take_the_whole_result_with_it() -> None:
    from dataclasses import replace as _replace

    unpriced = _replace(_stretch(100, 1.0), prices={})
    merged = replay_segments([_stretch(0, 1.0), unpriced])
    assert merged.status == "ok" and merged.segments == 1 and merged.skipped_segments == [(1, "missing_prices")]
    assert replay_segments([unpriced]).reason == "missing_prices"  # alone it is still refused, never made up


def test_a_stretch_that_cannot_be_balanced_takes_the_adjusted_difference_of_the_period_away() -> None:
    from dataclasses import replace as _replace

    open_end = _replace(_stretch(100, 1.0), end_soc=None)  # no level at its end: the energy cannot be balanced
    merged = replay_segments([_stretch(0, 1.0), open_end])
    assert merged.status == "ok" and merged.comparable is True and "energy_not_balanced" in merged.warnings
    assert all(d["adjusted_cost"] is None for d in merged.differences.values())
    assert merged.differences["cjp_asap"]["cost"] == pytest.approx(replay(_stretch(0, 1.0)).differences["cjp_asap"]["cost"] + replay(open_end).differences["cjp_asap"]["cost"])
