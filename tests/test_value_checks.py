"""The sanity checks of sensor values (management/value_checks.py): the verdicts, the units judged as prices, and no recorder.

The same file is in BattMind and Cable Juice Planner (only the package import path differs).
"""
from __future__ import annotations

import math

import pytest
from homeassistant.core import HomeAssistant

from custom_components.cable_juice_planner.management.value_checks import (
    KW_LOOKING_MAX,
    POWER_SCALE_DAYS,
    VALUE_MIN_SAMPLES,
    VALUE_RECHECK,
    async_recorded_numbers,
    battery_fraction_verdict,
    could_be_fraction,
    could_be_kw,
    is_price_per_wh,
    number_text,
    power_scale_verdict,
    price_unit_wrong,
)

# ten values, some of them fractions, all in kW-looking range
KW_HISTORY = [0.5, 1.2, 3.4, 0.8, 2.2, 4.0, 1.1, 0.3, 2.5, 3.3]
# the same values as watts: whole numbers only (a whole-number meter tells nothing)
WHOLE_HISTORY = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
SHARE_HISTORY = [0.85, 0.9, 0.5, 0.97, 0.12, 0.33, 0.6, 0.7, 0.8, 0.99]


def test_the_constants_are_the_documented_values() -> None:
    assert POWER_SCALE_DAYS == 3
    assert VALUE_RECHECK.total_seconds() == 6 * 3600
    assert VALUE_MIN_SAMPLES == 10
    assert KW_LOOKING_MAX == 50.0


# --------------------------------------------------------------------------- #
# power_scale_verdict: a power without a unit, read as W
# --------------------------------------------------------------------------- #


def test_fractions_that_never_reach_fifty_look_like_kilowatts() -> None:
    assert power_scale_verdict(KW_HISTORY) is True


def test_a_value_above_fifty_means_watts() -> None:
    assert power_scale_verdict([*KW_HISTORY, 50.5]) is False
    assert power_scale_verdict([60.0, 0.5, 0.7, 1.3, 0.2, 0.9, 1.1, 0.4, 0.6, 0.8]) is False


def test_exactly_fifty_still_looks_like_kilowatts() -> None:
    assert power_scale_verdict([*KW_HISTORY, 50.0]) is True


def test_too_few_numbers_cannot_tell() -> None:
    assert power_scale_verdict(KW_HISTORY[: VALUE_MIN_SAMPLES - 1]) is None
    assert power_scale_verdict([]) is None


def test_whole_numbers_cannot_tell() -> None:
    assert power_scale_verdict(WHOLE_HISTORY) is None


def test_all_zero_cannot_tell() -> None:
    assert power_scale_verdict([0.0] * 20) is None


def test_only_finite_numbers_count() -> None:
    values = [math.nan, math.inf, -math.inf, *KW_HISTORY[: VALUE_MIN_SAMPLES - 1]]
    assert power_scale_verdict(values) is None
    assert power_scale_verdict([math.inf, *KW_HISTORY]) is True


def test_the_sign_does_not_matter() -> None:
    assert power_scale_verdict([-value for value in KW_HISTORY]) is True


# --------------------------------------------------------------------------- #
# battery_fraction_verdict: a battery level in % that is a share
# --------------------------------------------------------------------------- #


def test_a_share_between_zero_and_one_is_flagged() -> None:
    assert battery_fraction_verdict(SHARE_HISTORY) is True


def test_a_value_above_one_means_percent() -> None:
    assert battery_fraction_verdict([*SHARE_HISTORY, 1.5]) is False
    assert battery_fraction_verdict([55.0, 0.5, 0.7, 0.2, 0.9, 0.3, 0.6, 0.8, 0.4, 0.1]) is False


def test_exactly_one_is_still_a_share() -> None:
    assert battery_fraction_verdict([*SHARE_HISTORY, 1.0]) is True


def test_a_negative_value_cannot_tell() -> None:
    assert battery_fraction_verdict([-0.5, *SHARE_HISTORY]) is None


def test_only_zero_and_one_cannot_tell() -> None:
    assert battery_fraction_verdict([0, 1] * 10) is None


def test_too_few_numbers_or_no_change_cannot_tell() -> None:
    assert battery_fraction_verdict(SHARE_HISTORY[: VALUE_MIN_SAMPLES - 1]) is None
    assert battery_fraction_verdict([0.5] * 20) is None


def test_non_finite_values_are_left_out() -> None:
    assert battery_fraction_verdict([math.nan, *SHARE_HISTORY]) is True
    assert battery_fraction_verdict([math.nan] * 3) is None


# --------------------------------------------------------------------------- #
# the state now: could it be the wrong scale (True) or is it certainly not (False)?
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("state", "expected"),
    [("0.5", True), ("50", True), ("0", True), ("-60", False), ("50.5", False), ("abc", None), ("unavailable", None), ("nan", None), (None, None)],
)
def test_could_be_kw(state, expected) -> None:
    assert could_be_kw(state) is expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [("0.85", True), ("0", True), ("1", True), ("1.5", False), ("85", False), ("-0.1", None), ("x", None), ("unknown", None), (None, None)],
)
def test_could_be_fraction(state, expected) -> None:
    assert could_be_fraction(state) is expected


# --------------------------------------------------------------------------- #
# price_unit_wrong: a price unit the prices are not read in (subunit or per MWh)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("unit", "wrong"),
    [
        ("øre/kWh", True),
        ("Øre/kWh", True),
        ("ct/kWh", True),
        ("cent/kWh", True),
        ("c/kWh", True),
        ("GBp/kWh", True),
        ("kr/MWh", True),
        ("DKK/MWh", True),
        ("EUR/MWh", True),
        ("DKK/kWh", False),
        ("kr/kWh", False),
        ("EUR/kWh", False),
        ("None", False),
        (None, False),
        ("", False),
    ],
)
def test_a_price_unit_that_is_a_subunit_or_per_mwh_is_wrong(unit, wrong) -> None:
    assert price_unit_wrong(unit) is wrong


def test_price_per_wh_is_wrong() -> None:
    assert is_price_per_wh("DKK/Wh")
    assert price_unit_wrong("DKK/Wh")
    assert price_unit_wrong("Øre/Wh")
    assert not is_price_per_wh("DKK/kWh")


# --------------------------------------------------------------------------- #
# number_text: the numbers of a repair text in the language of Home Assistant
# --------------------------------------------------------------------------- #


def test_number_text() -> None:
    assert number_text(4.235, "en") in ("4.24", "4.23")
    assert number_text(0.857, "en") == "0.857"
    assert number_text(38.24, "da") == "38,2"
    assert number_text(1234.5, "en") in ("1234", "1235")
    assert number_text(2.5, None) == "2.5"


# --------------------------------------------------------------------------- #
# the recorder: none without one, so the verdict stays unclear
# --------------------------------------------------------------------------- #


async def test_without_a_recorder_there_are_no_numbers(hass: HomeAssistant) -> None:
    assert "recorder" not in hass.config.components
    assert await async_recorded_numbers(hass, ["sensor.x"]) is None


async def test_no_entities_means_no_numbers(hass: HomeAssistant) -> None:
    hass.config.components.add("recorder")
    assert await async_recorded_numbers(hass, []) is None
