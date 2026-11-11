"""What-if replay inputs with connections estimated from the battery level statistics (engine/whatif/derive.py)."""
from __future__ import annotations

import pytest

from custom_components.cable_juice_planner.engine.whatif.inputs import build_replay_input
from custom_components.cable_juice_planner.engine.whatif.replay import replay

H = 3600.0
T0 = 1_700_000_000 - 1_700_000_000 % 3600


def hour(n: float) -> float:
    return T0 + n * H


DERIVED = {
    "status": "ok", "reason": None,
    "windows": [
        {"start": hour(0), "end": hour(8), "soc_in": 50.0, "soc_out": 70.0},
        {"start": hour(24), "end": hour(32), "soc_in": 55.0, "soc_out": 75.0},
        {"start": hour(48), "end": hour(56), "soc_in": 60.0, "soc_out": 80.0},
    ],
    "anchor": {"start": hour(100), "soc_in": 65.0},
    "validation": {"passed": True, "checked": 30, "recall": 0.9},
}


def build(**overrides):
    kwargs = dict(
        windows=[{"start": hour(100), "end": hour(106)}, {"start": hour(120), "end": hour(126)}],
        prices={hour(i): 1.0 + (i % 24) / 24 for i in range(140)},
        drives=[{"start": hour(110), "end": hour(112), "km": 40.0, "used": 20.0}],
        public_sessions=[],
        period_start=hour(-1), period_end=hour(140),
        battery_kwh=60.0, max_kw=10.0, phases=3, start_soc=40.0, target_soc=80.0,
        loss_factor=lambda kw: 0.1, power_cap=lambda p, a, b, kw: kw,
        prices_averaged_since=hour(-100),
        start_soc_source="entered",
        derived=DERIVED,
    )
    kwargs.update(overrides)
    return build_replay_input(**kwargs)


def test_older_connections_come_first_with_the_level_and_the_net_use_between_them_from_the_statistics() -> None:
    data, coverage = build()
    assert [(w.start, w.derived) for w in data.windows] == [
        (hour(0), True), (hour(24), True), (hour(48), True), (hour(100), False), (hour(120), False),
    ]
    # the level at the first estimated connection replaces the entered one; the entered level belongs to the first measured one
    assert data.start_soc == 50.0 and data.provenance["start_soc"] == "battery_statistics"
    assert data.provenance["windows"] == "measured_and_estimated_from_battery_statistics"
    # 70 -> 55, 75 -> 60 and 80 -> 65 (the anchor): 15 % each; plus the measured drive between the measured connections
    assert sorted((e.ts, e.soc_percent) for e in data.events) == [
        (hour(16), 15.0), (hour(40), 15.0), (hour(78), 15.0), (hour(110), 20.0),
    ]
    assert coverage["windows"] == 5 and coverage["windows_measured"] == 2 and coverage["windows_derived"] == 3
    assert coverage["derived_drives"] == 3 and coverage["first_window"] == hour(0)
    assert "connections_estimated_from_battery_statistics" in data.warnings
    result = replay(data)
    assert result.status == "ok" and result.windows_replayed == 5
    assert [w.derived for w in result.smart.windows] == [True, True, True, False, False]
    # the level carried from an estimated connection into the next one is the replay's own, minus the net use
    assert result.smart.windows[1].soc_in == pytest.approx(result.smart.windows[0].soc_out - 15.0)


def test_estimated_connections_without_a_price_are_left_out_so_they_cannot_cost_the_whole_result() -> None:
    # no price reaches back to the first two estimated connections (hours 0-8 and 24-32): only the last one is kept
    prices = {hour(i): 1.0 + (i % 24) / 24 for i in range(140) if not (i < 33)}
    data, coverage = build(prices=prices)
    assert [(w.start, w.derived) for w in data.windows] == [(hour(48), True), (hour(100), False), (hour(120), False)]
    assert coverage["estimated_unpriced_left_out"] == 2 and coverage["windows_derived"] == 1
    assert "estimated_connections_without_prices_left_out" in data.warnings and "connections_estimated_from_battery_statistics" in data.warnings
    # the level and the use come from the kept ones only: the start is the level at the first kept one, the use before the first measured one stays
    assert data.start_soc == 60.0 and [(e.ts, e.soc_percent) for e in data.events if e.ts < hour(100)] == [(hour(78), 15.0)]
    result = replay(data)
    assert result.status == "ok", result.reason
    # a share of connections without prices that would have failed the replay (fewer than half priced) with them in
    unpriced_all = {hour(i): 1.0 for i in range(96, 140)}  # only the measured connections have prices
    kept, _ = build(prices=unpriced_all)
    assert [w.derived for w in kept.windows] == [False, False] and kept.start_soc == 40.0 and kept.provenance["start_soc"] == "entered"
    assert not [e for e in kept.events if e.ts < hour(100)] and replay(kept).status == "ok"


def test_a_recorded_drive_replaces_the_net_use_in_the_gap_before_the_first_measured_connection() -> None:
    data, coverage = build(drives=[{"start": hour(110), "end": hour(112), "km": 40.0, "used": 20.0}, {"start": hour(70), "end": hour(72), "km": 30.0, "used": 18.0}])
    drives = sorted((e.ts, e.soc_percent) for e in data.events)
    assert (hour(70), 18.0) in drives and (hour(78), 15.0) not in drives
    assert coverage["derived_drives"] == 2


def test_a_recorded_drive_inside_the_estimated_period_is_not_counted_twice() -> None:
    data, coverage = build(drives=[{"start": hour(110), "end": hour(112), "km": 40.0, "used": 20.0}, {"start": hour(30), "end": hour(31), "km": 30.0, "used": 14.0}])
    assert (hour(30), 14.0) not in [(e.ts, e.soc_percent) for e in data.events]
    assert coverage["drives_replaced_by_derived"] == 1


def test_estimated_connections_that_overlap_a_measured_one_or_lie_outside_the_period_are_left_out() -> None:
    derived = {**DERIVED, "windows": [*DERIVED["windows"], {"start": hour(90), "end": hour(102), "soc_in": 60.0, "soc_out": 80.0}]}
    data, _ = build(derived=derived, period_start=hour(20))
    assert [w.start for w in data.windows if w.derived] == [hour(24), hour(48)]
    # the first of those starts the replay: its level is the one from the statistics
    assert data.start_soc == 55.0


def test_invalid_or_overlapping_estimated_entries_are_counted_and_not_used() -> None:
    bad = [
        {"start": hour(0), "end": hour(8), "soc_in": 50.0, "soc_out": 70.0},
        {"start": hour(4), "end": hour(12), "soc_in": 50.0, "soc_out": 70.0},  # overlaps the first
        {"start": hour(24), "end": hour(20), "soc_in": 50.0, "soc_out": 70.0},  # ends before it begins
        {"start": hour(30), "end": hour(32), "soc_in": 150.0, "soc_out": 70.0},  # impossible level
        {"start": hour(40), "end": hour(44), "soc_in": float("nan"), "soc_out": 70.0},
    ]
    data, coverage = build(derived={**DERIVED, "windows": bad})
    assert [w.start for w in data.windows if w.derived] == [hour(0)]
    assert coverage["derived_entries_invalid"] == 4


def test_estimates_that_were_not_validated_are_not_used_and_the_entered_level_stays() -> None:
    data, coverage = build(derived={"status": "unavailable", "reason": "not_validated", "windows": [], "anchor": None, "validation": {"passed": False}})
    assert [w.derived for w in data.windows] == [False, False] and data.start_soc == 40.0 and data.provenance["start_soc"] == "entered"
    assert coverage["windows_derived"] == 0 and coverage["derived_reason"] == "not_validated"
    assert "older_connections_not_validated" in data.warnings
    assert "connections_estimated_from_battery_statistics" not in data.warnings
    # nothing stored at all: no change to the measured replay
    plain, plain_coverage = build(derived=None)
    assert plain.start_soc == 40.0 and plain_coverage["windows_derived"] == 0 and plain_coverage["derived_status"] is None


def test_an_anchor_for_another_first_connection_or_without_a_level_means_no_estimated_connections_at_all() -> None:
    # the use in the gap before the first measured connection would be missing: that is not a demand of zero
    for anchor in ({"start": hour(99), "soc_in": 65.0}, None, {"start": hour(100), "soc_in": float("nan")}):
        data, coverage = build(derived={**DERIVED, "anchor": anchor})
        assert [w.derived for w in data.windows] == [False, False] and data.start_soc == 40.0
        assert coverage["windows_derived"] == 0 and coverage["derived_reason"] == "anchor_mismatch"
        assert "connections_estimated_from_battery_statistics" not in data.warnings
    # a first measured connection that was left out (the drive history is full) is another first connection
    data, coverage = build(windows=[{"start": hour(100), "end": hour(106)}, {"start": hour(120), "end": hour(126)}], period_start=hour(110))
    assert coverage["windows_derived"] == 0 and coverage["windows_measured"] == 1


def test_a_gap_where_the_level_did_not_fall_is_no_use_and_is_counted() -> None:
    windows = [
        {"start": hour(0), "end": hour(8), "soc_in": 50.0, "soc_out": 60.0},
        {"start": hour(24), "end": hour(32), "soc_in": 70.0, "soc_out": 75.0},  # the level rose while away: no net use
    ]
    data, coverage = build(derived={**DERIVED, "windows": windows, "anchor": {"start": hour(100), "soc_in": 75.0}})
    assert coverage["derived_gaps_without_use"] == 2 and coverage["derived_drives"] == 0  # between them and before the first measured one
