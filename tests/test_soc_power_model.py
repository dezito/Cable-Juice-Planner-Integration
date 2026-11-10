"""SOC-dependent vehicle charging power model."""
from __future__ import annotations

import datetime

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.engine import CJPEngine
from custom_components.cable_juice_planner.engine.loss import LOSS_SEGMENT_VERSION
from custom_components.cable_juice_planner.engine.soc_power import (
    build_soc_power_model,
    planning_power_cap,
)

from .test_engine import DAY, OPTIONS, TEXTS, snapshot


def _session(
    start: float,
    phases: int = 3,
    low_kw: float = 10.0,
    high_kw: float = 5.0,
    power_range: float = 100.0,
) -> list[dict]:
    return [
        {
            "start": start,
            "end": start + 600,
            "soc_start": 60.0,
            "soc_end": 70.0,
            "kw": low_kw,
            "phases": phases,
            "phase_observed": True,
            "power_min_w": low_kw * 1000 - 50,
            "power_max_w": low_kw * 1000 + 50,
            "source": "history",
        },
        {
            "start": start + 900,
            "end": start + 1500,
            "soc_start": 80.0,
            "soc_end": 90.0,
            "kw": high_kw,
            "phases": phases,
            "phase_observed": True,
            "power_min_w": high_kw * 1000 - power_range / 2,
            "power_max_w": high_kw * 1000 + power_range / 2,
            "source": "history",
        },
    ]


def test_learns_a_taper_only_after_three_distinct_stable_sessions() -> None:
    model = build_soc_power_model([
        *_session(1_760_000_000.0),
        *_session(1_760_086_400.0),
        *_session(1_760_172_800.0),
    ])

    assert model["status"] == "ready"
    assert model["bands"] == [
        {"phases": 3, "soc_start": 60, "soc_end": 70, "sessions": 0, "sample_sessions": 3, "cap_kw": None},
        {"phases": 3, "soc_start": 80, "soc_end": 90, "sessions": 3, "sample_sessions": 3, "cap_kw": 5.0},
    ]


def test_insufficient_or_unstable_sessions_do_not_create_a_cap() -> None:
    two_sessions = build_soc_power_model([*_session(1_760_000_000.0), *_session(1_760_086_400.0)])
    unstable = build_soc_power_model([
        *_session(1_760_000_000.0, power_range=1000.0),
        *_session(1_760_086_400.0, power_range=1000.0),
        *_session(1_760_172_800.0, power_range=1000.0),
    ])
    confirmed_raw = [
        *_session(1_760_000_000.0),
        *_session(1_760_086_400.0),
        *_session(1_760_172_800.0),
    ]
    unknown_phases = build_soc_power_model([{**segment, "phase_observed": False} for segment in confirmed_raw])
    assert two_sessions["status"] == "learning"
    assert all(row["cap_kw"] is None for row in two_sessions["bands"])
    assert unstable["status"] == "learning"
    assert all(row["cap_kw"] is None for row in unstable["bands"])
    assert unknown_phases["status"] == "no_data"


def test_statistics_give_a_marked_start_cap_until_own_data_exists() -> None:
    statistics = [{**segment, "source": "statistics", "phase_observed": False} for start in (1_760_000_000.0, 1_760_086_400.0, 1_760_172_800.0) for segment in _session(start)]
    model = build_soc_power_model(statistics)
    capped = next(row for row in model["bands"] if row["cap_kw"] is not None)
    assert capped["source"] == "statistics"
    assert capped["cap_kw"] == 5.0

    own = [segment for start in (1_760_000_000.0, 1_760_086_400.0, 1_760_172_800.0) for segment in _session(start, high_kw=6.0)]
    combined = build_soc_power_model([*own, *statistics])
    row = next(row for row in combined["bands"] if row["soc_start"] == 80)
    assert row["cap_kw"] == 6.0
    assert "source" not in row


def test_merge_statistics_segments_dedups() -> None:
    from custom_components.cable_juice_planner.engine.soc_power import merge_statistics_segments

    seg = {"start": 1_760_000_000.0, "end": 1_760_000_600.0, "phases": 1, "kw": 5.0, "source": "statistics"}
    merged = merge_statistics_segments([seg], [dict(seg)], 1_760_001_000.0)
    assert len(merged) == 1


def test_soc_reset_splits_sessions_and_cannot_create_a_false_taper() -> None:
    segments = []
    for start in (1_760_000_000.0, 1_760_086_400.0, 1_760_172_800.0):
        low_then_high = _session(start)
        high_soc = {**low_then_high[1], "start": start, "end": start + 600}
        reset_to_low_soc = {**low_then_high[0], "start": start + 900, "end": start + 1500}
        segments.extend((high_soc, reset_to_low_soc))

    model = build_soc_power_model(segments)

    high_soc_band = next(row for row in model["bands"] if row["soc_start"] == 80)
    assert model["status"] == "learning"
    assert high_soc_band["cap_kw"] is None
    assert high_soc_band["sessions"] == 0


def test_model_rebuilds_from_the_persisted_charging_loss_database() -> None:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    segments = [
        *_session(1_760_000_000.0),
        *_session(1_760_086_400.0),
        *_session(1_760_172_800.0),
    ]

    def load_and_publish() -> None:
        engine.load_charging_loss_db({
            "segments": [],
            "soc_power_segments": segments,
            "segment_version": LOSS_SEGMENT_VERSION,
        })
        engine.publish_soc_power_model()

    timestamp = datetime.datetime.combine(DAY, datetime.time(12, 0))
    result = engine.run_job(snapshot(timestamp), load_and_publish)

    assert result.error is None, result.error
    assert result.states["soc_charging_power_model"] == "ready"
    assert result.attributes["soc_charging_power_model"]["bands"][-1]["cap_kw"] == 5.0
    assert engine.soc_power_model["status"] == "ready"
    assert engine.soc_power_model["bands"][-1]["cap_kw"] == 5.0


def test_power_model_is_phase_specific_and_only_clamps_crossed_soc_bands() -> None:
    model = build_soc_power_model([
        *_session(1_760_000_000.0),
        *_session(1_760_086_400.0),
        *_session(1_760_172_800.0),
        *_session(1_760_259_200.0, phases=1, low_kw=3.0, high_kw=1.5),
        *_session(1_760_345_600.0, phases=1, low_kw=3.0, high_kw=1.5),
        *_session(1_760_432_000.0, phases=1, low_kw=3.0, high_kw=1.5),
    ])

    assert planning_power_cap(model, 3, 78.0, 82.0, 11.0) == 5.0
    assert planning_power_cap(model, 1, 80.0, 85.0, 7.0) == 1.5
    assert planning_power_cap(model, 3, 50.0, 60.0, 11.0) == 11.0
    assert planning_power_cap(model, 3, 80.0, 90.0, 4.0) == 4.0


def test_interval_crossing_multiple_soc_bands_uses_the_lowest_learned_cap() -> None:
    model = {
        "bands": [
            {"phases": 3, "soc_start": 70, "soc_end": 80, "cap_kw": 8.0},
            {"phases": 3, "soc_start": 80, "soc_end": 90, "cap_kw": 5.0},
        ],
    }

    assert planning_power_cap(model, 3, 75.0, 95.0, 11.0) == 5.0


def test_planner_applies_the_cap_only_when_enabled_and_respects_short_intervals() -> None:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    engine.soc_power_model = {
        "status": "ready",
        "min_sessions": 3,
        "bands": [{"phases": 3, "soc_start": 80, "soc_end": 90, "sessions": 3, "sample_sessions": 3, "cap_kw": 5.0}],
    }
    timestamp = datetime.datetime.combine(DAY, datetime.time(12, 0))
    engine.price_period_end = lambda _timestamp: timestamp + datetime.timedelta(minutes=15)

    def capacity(enabled: bool) -> float:
        snap = snapshot(timestamp)
        snap.settings["learn_soc_charge_power"] = enabled
        result = engine.run_job(snap, lambda: engine.period_charge_capacity(timestamp, 80.0))
        assert result.error is None, result.error
        return result.value[0]

    assert capacity(False) == 2.76
    assert capacity(True) == 1.25


def test_planner_keeps_a_very_short_interval_after_applying_the_cap() -> None:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    engine.soc_power_model = {
        "status": "ready",
        "min_sessions": 3,
        "bands": [{"phases": 3, "soc_start": 80, "soc_end": 90, "sessions": 3, "sample_sessions": 3, "cap_kw": 5.0}],
    }
    timestamp = datetime.datetime.combine(DAY, datetime.time(12, 0))
    engine.price_period_end = lambda _timestamp: timestamp + datetime.timedelta(minutes=2)
    snap = snapshot(timestamp)
    snap.settings["learn_soc_charge_power"] = True
    charge_hours = {}

    result = engine.run_job(
        snap,
        lambda: engine.add_to_charge_hours(
            charge_hours,
            {},
            1.0,
            0.0,
            0.0,
            timestamp,
            1.0,
            False,
            False,
            False,
            battery_level=80.0,
            max_recommended_battery_level=100.0,
        ),
    )

    assert result.error is None, result.error
    assert charge_hours[timestamp]["kWh"] > 0


def test_planner_subtracts_existing_interval_energy_from_the_learned_capacity() -> None:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    engine.soc_power_model = {
        "status": "ready",
        "min_sessions": 3,
        "bands": [{"phases": 3, "soc_start": 80, "soc_end": 90, "sessions": 3, "sample_sessions": 3, "cap_kw": 5.0}],
    }
    timestamp = datetime.datetime.combine(DAY, datetime.time(12, 0))
    engine.price_period_end = lambda _timestamp: timestamp + datetime.timedelta(minutes=15)
    snap = snapshot(timestamp)
    snap.settings["learn_soc_charge_power"] = True
    charge_hours = {
        timestamp: {
            "kWh": 2.0,
            "Cost": 0.0,
            "battery_level": 0.0,
            "Price": 1.0,
            "ChargingAmps": 16,
        },
    }

    result = engine.run_job(
        snap,
        lambda: engine.add_to_charge_hours(
            charge_hours,
            {},
            1.0,
            0.0,
            2.0,
            timestamp,
            1.0,
            False,
            False,
            False,
            battery_level=80.0,
            max_recommended_battery_level=100.0,
        ),
    )

    assert result.error is None, result.error
    assert charge_hours[timestamp]["kWh"] == 2.0
