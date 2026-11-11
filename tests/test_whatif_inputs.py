"""What-if replay inputs: CJP's stored data to a replay, with coverage and without invented values."""
from __future__ import annotations

import datetime
import math
import zoneinfo

import pytest

from custom_components.cable_juice_planner.engine.soc_power import planning_power_cap
from custom_components.cable_juice_planner.engine.whatif.inputs import (
    build_replay_input,
    loss_factor_from_table,
    power_cap_from_model,
)
from custom_components.cable_juice_planner.engine.whatif.replay import replay

H = 3600.0
T0 = 1_700_000_000 - 1_700_000_000 % 3600
CPH = zoneinfo.ZoneInfo("Europe/Copenhagen")


def hour(n: float) -> float:
    return T0 + n * H


def build(**overrides):
    kwargs = dict(
        windows=[{"start": hour(0), "end": hour(6)}, {"start": hour(20), "end": hour(26)}],
        prices={hour(i): 1.0 for i in range(30)},
        drives=[{"start": hour(10), "end": hour(12), "km": 40.0, "used": 20.0}],
        public_sessions=[],
        period_start=hour(-1), period_end=hour(40),
        battery_kwh=60.0, max_kw=10.0, phases=3, start_soc=50.0, target_soc=80.0,
        loss_factor=lambda kw: 0.1, power_cap=lambda p, a, b, kw: kw,
        prices_averaged_since=hour(-100),
        start_soc_source="user",
    )
    kwargs.update(overrides)
    return build_replay_input(**kwargs)


def test_stored_data_becomes_a_replay_with_coverage() -> None:
    data, coverage = build()
    assert [(w.start, w.end) for w in data.windows] == [(hour(0), hour(6)), (hour(20), hour(26))]
    assert [(e.kind, e.soc_percent) for e in data.events] == [("drive", 20.0)]
    assert coverage["price_hours_needed"] == 12 and coverage["price_hours_missing"] == 0
    assert coverage["drives"] == 1 and coverage["public_charges"] == 0
    assert coverage["connection_gaps"] == 1 and coverage["connection_gaps_without_drive"] == 0
    assert data.provenance["prices"] == "measured" and data.provenance["start_soc"] == "user"
    result = replay(data)
    assert result.status == "ok" and result.windows_replayed == 2
    assert result.smart.windows[1].soc_in == pytest.approx(result.smart.windows[0].soc_out - 20.0)


def test_open_and_outside_windows_are_not_used_and_are_counted() -> None:
    windows = [
        {"start": hour(-10), "end": hour(-8)},  # before the period
        {"start": hour(0), "end": hour(6)},
        {"start": hour(20), "end": None},  # still plugged in
        {"start": hour(30), "end": hour(50)},  # ends after the period
    ]
    data, coverage = build(windows=windows)
    assert len(data.windows) == 1
    assert coverage["windows_outside_period"] == 2 and coverage["windows_still_open"] == 1


def test_missing_or_invalid_prices_are_not_filled() -> None:
    prices = {hour(i): 1.0 for i in range(30)}
    del prices[hour(22)]
    prices[hour(23)] = math.nan
    prices[hour(24) + 900.0] = 0.01  # a quarter-hour key
    prices["not a number"] = 3.0
    data, coverage = build(prices=prices)
    assert coverage["price_hours_missing"] == 2
    assert hour(24) + 900.0 not in data.prices and hour(22) not in data.prices and hour(23) not in data.prices
    result = replay(data)
    assert result.status == "partial" and result.reason == "missing_prices" and result.windows_replayed == 1


def test_start_soc_is_never_defaulted() -> None:
    data, _ = build(start_soc=None, start_soc_source="unavailable")
    result = replay(data)
    assert result.status == "unavailable" and result.reason == "no_start_soc"


def test_drives_and_public_charges_outside_the_replayed_span_are_left_out() -> None:
    drives = [
        {"start": hour(-5), "used": 10.0},  # before the first window
        {"start": hour(10), "used": 20.0},
        {"start": hour(30), "used": 30.0},  # after the last window
        {"start": hour(11), "used": math.nan},
        {"start": None, "used": 5.0},
    ]
    sessions = [{"ts": hour(15), "kwh": 6.0}, {"ts": hour(15), "kwh": 0.0}, {"ts": hour(16), "kwh": math.inf}]
    data, coverage = build(drives=drives, public_sessions=sessions)
    assert coverage["drives"] == 1 and coverage["public_charges"] == 1
    # a public charge is already inside the net battery use of its drive: it is counted, not replayed
    assert [e.kind for e in data.events] == ["drive"]


def test_prices_before_the_averaging_change_are_labelled_unverified() -> None:
    data, coverage = build(prices_averaged_since=hour(3))
    assert coverage["price_hours_legacy_unverified"] > 0
    assert data.provenance["prices"] == "measured_partly_unverified"
    unknown, coverage = build(prices_averaged_since=None)
    assert coverage["price_hours_legacy_unverified"] == coverage["price_hours_covered"]


def test_loss_factor_table_uses_the_nearest_power_and_never_invents_one() -> None:
    factor = loss_factor_from_table({1.4: 0.20, 3.7: 0.12, 11.0: 0.08, 7.0: math.nan, 5.0: -1.0})
    assert factor(1.0) == 0.20 and factor(4.0) == 0.12 and factor(10.0) == 0.08
    assert loss_factor_from_table({3.7: 0.12, 7.4: 0.08})(5.55) == 0.12  # tie: the lower power
    assert math.isnan(loss_factor_from_table({})(7.0))
    assert loss_factor_from_table({}, default=0.1)(7.0) == 0.1
    data, _ = build(loss_factor=loss_factor_from_table({}))
    assert replay(data).reason == "invalid_loss_model"


def test_learned_soc_power_model_limits_the_replay() -> None:
    model = {"bands": [
        {"phases": 3, "soc_start": 70, "soc_end": 80, "cap_kw": 3.0},
        {"phases": 3, "soc_start": 80, "soc_end": 90, "cap_kw": 2.0},
    ]}
    short = [{"start": hour(0), "end": hour(5)}]
    free, _ = build(start_soc=20.0, windows=short, drives=[])
    capped, _ = build(start_soc=20.0, windows=short, drives=[], power_cap=power_cap_from_model(model, planning_power_cap))
    assert power_cap_from_model(None, planning_power_cap)(3, 0.0, 100.0, 11.0) == 11.0
    free_result, capped_result = replay(free), replay(capped)
    # 20 -> 80 % in 5 hours is possible at 10 kW, but not when the car accepts only 3 kW from 70 %
    assert free_result.smart.unmet_target_kwh == pytest.approx(0.0)
    assert capped_result.smart.unmet_target_kwh > 0.0
    assert capped_result.smart.charged_kwh < free_result.smart.charged_kwh
    assert capped_result.max_conservation_error_kwh < 1e-6


def test_a_dst_change_keeps_every_real_hour() -> None:
    """Plugged in Saturday 18:00 (CEST) to Sunday 08:00 (CET) over the autumn change: 15 real hours."""
    start = datetime.datetime(2026, 10, 24, 18, tzinfo=CPH).timestamp()
    end = datetime.datetime(2026, 10, 25, 8, tzinfo=CPH).timestamp()
    assert (end - start) / H == 15
    prices = {start + i * H: 1.0 for i in range(15)}
    data, coverage = build(
        windows=[{"start": start, "end": end}], prices=prices, drives=[],
        period_start=start - H, period_end=end + H, start_soc=0.0, target_soc=100.0,
    )
    assert coverage["price_hours_needed"] == 15 and coverage["price_hours_missing"] == 0
    result = replay(data)
    assert result.status == "ok"
    # 60 kWh * 1.1 from the charger, regardless of the repeated 02:00-03:00 local hour
    assert result.asap.grid_kwh == pytest.approx(66.0)


def test_everything_left_out_is_counted_and_warned_about() -> None:
    data, coverage = build(
        windows=[{"start": hour(0), "end": hour(6)}, {"start": hour(8), "end": hour(7)}, "junk"],
        prices={**{hour(i): 1.0 for i in range(6)}, "x": 1.0, hour(7) + 5: 1.0, hour(8): None},
        drives=[{"start": hour(1), "used": 10.0}, {"start": hour(2), "used": None}, {"start": hour(2), "used": -1.0},
                {"start": hour(-9), "used": 5.0}],
        public_sessions=[{"ts": hour(3), "kwh": 5.0}, {"ts": hour(3), "kwh": math.nan}, {"ts": hour(30), "kwh": 5.0}],
        prices_averaged_since=hour(3),
    )
    assert coverage["windows"] == 1 and coverage["windows_invalid"] == 1
    assert coverage["price_entries_invalid"] == 3
    assert coverage["drives"] == 1 and coverage["drives_invalid"] == 2 and coverage["drives_outside_replay"] == 1
    assert coverage["public_charges"] == 1 and coverage["public_charges_invalid"] == 1
    assert set(data.warnings) == {"prices_legacy_unverified", "invalid_input_entries_left_out"}
    assert set(replay(data).warnings) >= set(data.warnings)


def test_clean_recent_data_has_no_warnings() -> None:
    data, _ = build(prices_averaged_since=hour(-100))
    assert data.warnings == ()


def test_a_public_charge_is_never_added_to_the_battery_level_a_second_time() -> None:
    """The stored drive is the level at unplug minus the level at the next plug-in: public charging is inside it."""
    # 80 % at unplug, 60 % driven, 40 % bought on the way: 60 % at the next plug-in = a net use of 20 %
    drive = {"start": hour(10), "end": hour(12), "km": 120.0, "used": 20.0}
    public = {"ts": hour(11), "kwh": 24.0}  # 40 % of 60 kWh
    data, coverage = build(drives=[drive], public_sessions=[public], start_soc=80.0, target_soc=80.0)
    assert [(e.kind, e.soc_percent) for e in data.events] == [("drive", 20.0)]
    result = replay(data)
    assert result.status == "ok" and result.smart.windows[1].soc_in == pytest.approx(60.0)
    assert coverage["public_charges"] == 1 and coverage["public_charges_without_drive"] == 0
    assert "public_charge_without_drive" not in data.warnings


def test_a_public_charge_without_a_recorded_drive_is_flagged() -> None:
    data, coverage = build(drives=[], public_sessions=[{"ts": hour(10), "kwh": 20.0}])
    assert coverage["public_charges_without_drive"] == 1 and "public_charge_without_drive" in data.warnings
    assert coverage["connection_gaps"] == 1 and coverage["connection_gaps_without_drive"] == 1
    assert "many_connections_without_drive" in data.warnings


def test_a_full_drive_history_leaves_out_connections_before_it() -> None:
    drives = [{"start": hour(21), "used": 10.0}, {"start": hour(40), "used": 10.0}]
    windows = [{"start": hour(0), "end": hour(6)}, {"start": hour(20), "end": hour(26)}, {"start": hour(30), "end": hour(36)}]
    full, coverage = build(windows=windows, drives=drives, drive_history_limit=2)
    assert [w.start for w in full.windows] == [hour(20), hour(30)] and coverage["windows_before_drive_history"] == 1
    assert "connections_before_drive_history_left_out" in full.warnings
    # a history that is not full simply began later: nothing is left out
    partial, coverage = build(windows=windows, drives=drives, drive_history_limit=400)
    assert len(partial.windows) == 3 and coverage["windows_before_drive_history"] == 0


def test_a_copied_autumn_hour_is_flagged() -> None:
    data, coverage = build(dst_copied_hours=[hour(3), hour(500)])
    assert coverage["price_hours_dst_copied"] == 1 and "prices_dst_hour_copied" in data.warnings
    assert data.provenance["prices"] == "measured_partly_unverified"


def test_the_window_selection_is_shared_and_reports_what_it_left_out() -> None:
    from custom_components.cable_juice_planner.engine.whatif.inputs import select_windows

    chosen, counts = select_windows(
        [{"start": hour(0), "end": hour(2)}, {"start": hour(5), "end": None}, {"end": hour(1)}, {"start": hour(9), "end": hour(8)}, "x"],
        hour(-1), hour(10),
    )
    assert [(w.start, w.end) for w in chosen] == [(hour(0), hour(2))]
    assert counts == {"outside": 0, "open": 1, "invalid": 2, "before_drive_history": 0}
