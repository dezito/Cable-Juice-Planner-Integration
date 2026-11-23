"""The test of the user's charger automation: the steps, the held current, the search for the current that works, the runner."""
from __future__ import annotations

import asyncio
import datetime
import json
import re
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event

from custom_components.cable_juice_planner.const import CHARGER_TEST_STATUSES, DOMAIN, build_config
from custom_components.cable_juice_planner.engine.control.charger_test import (
    MAX_ADJUSTMENTS,
    SKIP_REASONS,
    STOP_REASONS,
    amps_list,
    amps_unproven,
    build_charger_test_markdown,
    build_steps,
    car_phases_of,
    charger_step_text,
    judge_at_measured_voltage,
    judge_power,
    limit_test_value,
    measured_voltage,
    next_amps,
    overdraw_w,
    power_is_capped,
)
from custom_components.cable_juice_planner.management.automation_inputs import (
    AMPS_INPUTS,
    START_INPUTS,
    STOP_INPUTS,
    automations_using_blueprint,
    has_steps,
)
from custom_components.cable_juice_planner.management.dashboard_build import render_dashboard
from custom_components.cable_juice_planner.utils.formatting import translate

from .conftest import CHARGER_POWER, CHARGER_STATUS, EV_LIMIT, MOCK_OPTIONS, async_setup, async_unload_and_join, make_entry, set_source_states
from .test_dashboard import _own
from .test_engine import OPTIONS as ENGINE_OPTIONS

ACTION = "sensor.cjp_test_charger_action"
SWITCH = "switch.cjp_test_charger_test_run"
RESULT = "sensor.cjp_test_charger_test"
VOLTAGE = 230.0
TEXT_DIR = Path(__file__).parents[1] / "custom_components" / "cable_juice_planner" / "texts"
FOUND = "custom_components.cable_juice_planner.coordinator.charger_test.automations_using_blueprint"


def _steps(phases: int = 3, max_amp: int = 16, solar: bool = True) -> list[dict]:
    power_dict = {}
    for amps in range(6, 17):
        power_dict[amps * VOLTAGE] = {"amp": float(amps), "phase": 1.0, "watt": amps * VOLTAGE}
    for amps in range(5, max_amp + 1):
        watt = amps * VOLTAGE * phases
        if watt > 16 * VOLTAGE:
            power_dict[watt] = {"amp": float(amps), "phase": float(phases), "watt": watt}
    return build_steps(power_dict if solar else {}, max_amp, phases, VOLTAGE, solar)


# --- the steps ----------------------------------------------------------------------------------------------------


def test_the_steps_are_the_maximum_and_the_lowest_and_highest_step_of_each_number_of_phases() -> None:
    steps = {step["name"]: step for step in _steps()}
    assert list(steps) == ["max", "min", "single_max", "single_min"]
    assert (steps["max"]["phases"], steps["max"]["amps"], steps["max"]["search"]) == (3, 16, -1)
    assert (steps["min"]["phases"], steps["min"]["amps"], steps["min"]["search"]) == (3, 6, 1)  # 5 A on three phases is less than 16 A on one
    assert (steps["single_max"]["phases"], steps["single_max"]["amps"], steps["single_max"]["search"]) == (1, 16, -1)
    assert (steps["single_min"]["phases"], steps["single_min"]["amps"], steps["single_min"]["search"]) == (1, 6, 1)
    assert steps["max"]["expected_w"] == 16 * 3 * VOLTAGE and steps["single_min"]["expected_w"] == 6 * VOLTAGE


def test_without_solar_steps_only_the_maximum_is_a_step() -> None:
    assert [step["name"] for step in _steps(solar=False)] == ["max"]


def test_a_single_phase_charger_has_one_group() -> None:
    steps = _steps(phases=1)
    assert [step["name"] for step in steps] == ["max", "min"] and all(step["phases"] == 1 for step in steps)


def test_the_amps_are_put_on_the_first_phases() -> None:
    assert amps_list(10, 1) == [10, 0, 0] and amps_list(10, 3) == [10, 10, 10]


def test_the_power_must_match_the_amps_within_five_percent_and_150_watt() -> None:
    assert judge_power(3680.0, 3600.0) and judge_power(3680.0, 3800.0)
    assert not judge_power(3680.0, 4300.0)  # one amp more on three phases
    assert not judge_power(10350.0, 9660.0)  # 15 A on three phases, the charger gave 14 A
    assert not judge_power(3680.0, 0.0)
    assert judge_power(1380.0, 1250.0) and not judge_power(1380.0, 1000.0)


def _tries(*pairs) -> list[dict]:
    return [{"amps": amps, "ok": ok} for amps, ok in pairs]


def test_the_search_goes_two_amps_towards_the_middle_and_then_one_back() -> None:
    low = {"search": 1, "low": 6, "high": 16}
    assert next_amps(low, _tries((6, False))) == 8
    assert next_amps(low, _tries((6, False), (8, True))) == 7  # between the failing and the working one
    assert next_amps(low, _tries((6, False), (8, True), (7, True))) is None  # 7 works and 6 does not: done
    assert next_amps(low, _tries((6, False), (8, True), (7, False))) is None  # 8 is the lowest that works
    assert next_amps(low, _tries((6, False), (8, False))) == 10
    assert next_amps(low, _tries((6, True))) is None  # the step's own current works
    high = {"search": -1, "low": 6, "high": 16}
    assert next_amps(high, _tries((16, False))) == 14
    assert next_amps(high, _tries((16, False), (14, True))) == 15
    assert next_amps(high, _tries((16, False), (14, True), (15, False))) is None
    assert next_amps(high, _tries((16, False), (14, False), (12, True))) == 13


def test_power_clearly_above_the_published_amps_is_an_overdraw() -> None:
    assert overdraw_w(1380.0) == 1880.0  # at least 500 W
    assert overdraw_w(11040.0) == 13800.0  # 25 %
    assert 16 * 3 * VOLTAGE > overdraw_w(16 * VOLTAGE)  # a charger that does not switch to one phase


def test_a_car_that_charges_on_fewer_phases_is_found() -> None:
    assert car_phases_of(16, 3, VOLTAGE, 16 * VOLTAGE) == 1
    assert car_phases_of(16, 3, VOLTAGE, 16 * 2 * VOLTAGE) == 2
    assert car_phases_of(16, 3, VOLTAGE, 5000.0) is None  # matches no number of phases
    assert car_phases_of(16, 1, VOLTAGE, 16 * VOLTAGE) is None  # one phase can not be fewer


def _watts(*triples) -> list[dict]:
    return [{"amps": amps, "watt": watt, "ok": ok} for amps, watt, ok in triples]


def test_power_that_does_not_rise_with_the_amps_is_a_limit_of_the_car_or_the_charger() -> None:
    threshold = 5 * VOLTAGE
    capped = _watts((16, 8000, False), (14, 8000, False), (12, 8000, True), (13, 8000, False))
    assert power_is_capped(-1, capped, capped[2], threshold)
    rising = _watts((16, 0, False), (14, 9660, True), (15, 0, False))  # the higher currents gave nothing: not a cap
    assert not power_is_capped(-1, rising, rising[1], threshold)
    assert not power_is_capped(1, capped, capped[2], threshold)  # a lowest step searches upwards: higher currents are not "nearer"
    flat = _watts((6, 3000, False), (8, 3000, False), (10, 3050, False))  # charges, at the same power whatever the amps
    assert power_is_capped(1, flat, None, threshold)
    assert not power_is_capped(1, _watts((6, 0, False), (8, 0, False)), None, threshold)  # does not charge at all
    assert not power_is_capped(1, _watts((6, 3000, False)), None, threshold)  # one current says nothing


def test_the_voltage_is_measured_when_the_power_follows_the_amps_at_another_voltage() -> None:
    threshold = 5 * VOLTAGE
    # a debug export: the 1-phase steps of a Tesla on an Easee charger, 240 V set, the grid gave about 223 V under the load
    real = _watts((16, 3563, False), (14, 3131, False), (12, 2669, False), (10, 2246, False), (8, 1776, True), (6, 1348, True))
    assert measured_voltage(real, 1, 240.0, threshold) == pytest.approx(223.3, abs=0.5)
    # attempts that gave nothing do not count
    assert measured_voltage(_watts((16, 0, False), (12, 2669, True), (6, 1348, True)), 1, 240.0, threshold) == pytest.approx(223.5, abs=0.6)
    # a charger that always gives one amp less: the power per amp drifts with the amps
    short = _watts(*((amps, (amps - 1) * VOLTAGE, False) for amps in (16, 14, 12, 10, 8)))
    assert measured_voltage(short, 1, VOLTAGE, threshold) is None
    # two currents close together can't tell an amp short from a voltage
    assert measured_voltage(_watts((16, 15 * VOLTAGE, False), (14, 13 * VOLTAGE, False)), 1, VOLTAGE, threshold) is None
    assert measured_voltage(_watts((16, 3400, False)), 1, VOLTAGE, threshold) is None
    # a car that limits its power
    assert measured_voltage(_watts((16, 8000, False), (14, 8000, False), (12, 8000, True), (8, 5520, True)), 3, VOLTAGE, threshold) is None
    # a voltage far from the setting: a car on fewer phases than published, not the grid
    assert measured_voltage(_watts((16, 16 * VOLTAGE, False), (8, 8 * VOLTAGE, False)), 3, VOLTAGE, threshold) is None


def _export_step(name: str, phases: int, configured: int, *triples, status: str = "ok") -> dict:
    attempts = _watts(*triples)
    best = attempts[-1]
    return {
        "name": name, "status": status, "reason": None, "phases": phases, "configured_amps": configured, "amps": best["amps"], "watt": best["watt"],
        "expected_w": best["amps"] * phases * 240.0, "attempts": attempts, "seconds": 60, "limited": False,
    }


# a debug export: 240 V set, the grid gave about 229 V on three phases and 223 V on one; the 1-phase maximum ended on 8 A
EXPORT_STEPS = [
    _export_step("max", 3, 16, (16, 10967, True)),
    _export_step("min", 3, 6, (6, 4137, True)),
    _export_step("single_max", 1, 16, (16, 3563, False), (14, 3131, False), (12, 2669, False), (10, 2246, False), (8, 1776, True), status="adjusted"),
    _export_step("single_min", 1, 6, (6, 1348, True)),
    {"name": "stop", "status": "ok", "reason": None, "phases": 0, "configured_amps": 0, "amps": 0, "watt": 0, "expected_w": 0, "attempts": []},
]


def test_the_steps_are_judged_again_at_the_measured_voltage() -> None:
    threshold = 5 * 240.0
    judged = {step["name"]: step for step in judge_at_measured_voltage(EXPORT_STEPS, 240.0, threshold)}
    single = judged["single_max"]
    assert single["status"] == "ok" and single["amps"] == 16 and single["watt"] == 3563 and not single["limited"]
    assert single["voltage"] == pytest.approx(223.3, abs=0.5) and single["voltage_setting"] == 240.0
    assert judge_power(single["expected_w"], single["watt"]) and all(attempt["ok"] for attempt in single["attempts"])
    assert judged["single_min"]["status"] == "ok" and judged["single_min"]["voltage"] == single["voltage"]
    assert judged["max"]["status"] == "ok" and judged["max"]["voltage"] == pytest.approx(229.2, abs=0.5)
    assert judged["stop"] == EXPORT_STEPS[4]
    assert EXPORT_STEPS[2]["status"] == "adjusted" and "voltage" not in EXPORT_STEPS[2]  # a copy, not changed in place
    assert judge_at_measured_voltage(list(judged.values()), 240.0, threshold) == list(judged.values())  # judged once is judged


def test_a_voltage_near_the_setting_or_a_step_that_proves_nothing_is_left_as_it_is() -> None:
    threshold = 5 * VOLTAGE
    near = [_export_step("max", 3, 16, (16, 16 * 3 * 227, True)), _export_step("min", 3, 6, (6, 6 * 3 * 227, True))]
    assert judge_at_measured_voltage(near, VOLTAGE, threshold) == near  # 227 V is within 2 % of 230 V
    started = {**EXPORT_STEPS[0], "status": "adjusted", "already_charging": True}
    overdraw = {**EXPORT_STEPS[2], "status": "fail", "reason": "overdraw", "attempts": _watts((16, 11000, False))}
    judged = {step["name"]: step for step in judge_at_measured_voltage([started, EXPORT_STEPS[1], overdraw, EXPORT_STEPS[3]], 240.0, 5 * 240.0)}
    assert judged["max"]["status"] == "adjusted" and judged["max"]["voltage"]  # a start that is not proven stays a note
    assert judged["single_max"] == overdraw and "voltage" not in judged["single_min"]  # one current on one phase proves nothing


def _record(name: str, phases: int, *triples, status: str = "adjusted") -> dict:
    return {"name": name, "status": status, "phases": phases, "attempts": _watts(*triples)}


def test_the_amps_are_not_proven_when_the_power_was_the_same_at_every_current() -> None:
    threshold = 5 * VOLTAGE
    stuck = [  # a charger that stays at 6 A on three phases whatever is published
        _record("max", 3, (16, 4140, False), (14, 4140, False), (12, 4140, False)),
        _record("min", 3, (6, 4140, True), status="ok"),
        {"name": "stop", "status": "ok", "phases": 0, "attempts": []},
    ]
    assert amps_unproven(stuck, VOLTAGE, threshold)
    capped = [_record("max", 3, (16, 7360, False), (11, 7360, True)), _record("min", 3, (6, 4140, True), status="ok")]
    assert not amps_unproven(capped, VOLTAGE, threshold)  # a car that limits the power at the top: the lowest step follows the amps
    assert not amps_unproven([_record("max", 3, (16, 11040, True), status="ok")], VOLTAGE, threshold)  # one power asked for proves nothing
    paused = [_record("max", 3, (16, 4140, False), (14, 0, False)), _record("min", 3, (6, 4140, True), status="ok")]
    assert amps_unproven(paused, VOLTAGE, threshold)  # a current that gave nothing is not a power that followed the amps
    skipped = [_record("max", 3, (16, 4140, False)), _record("min", 3, (6, 2000, False), status="skip")]
    assert not amps_unproven(skipped, VOLTAGE, threshold)  # a step that was not judged does not count
    switching = [  # a charger that switches the phases but stays at 6 A: other powers on one and three phases, none per amps
        _record("max", 3, (16, 4140, False), (14, 4140, False)), _record("min", 3, (6, 4140, True), status="ok"),
        _record("single_max", 1, (16, 1380, False), (14, 1380, False)), _record("single_min", 1, (6, 1380, True), status="ok"),
    ]
    assert amps_unproven(switching, VOLTAGE, threshold)
    one_group_follows = [*switching[:2], _record("single_max", 1, (16, 3680, True), status="ok"), switching[3]]
    assert not amps_unproven(one_group_follows, VOLTAGE, threshold)


def test_a_limited_step_tells_the_highest_current_and_what_a_car_that_always_takes_less_needs() -> None:
    texts = _texts("en")
    t = lambda key, **fmt: translate(texts, key, **fmt)  # noqa: E731
    top = {"name": "max", "status": "adjusted", "limited": True, "phases": 3, "configured_amps": 25, "amps": 16, "watt": 11040,
           "expected_w": 11040, "attempts": _watts((25, 11040, False), (23, 11040, False))}
    text = charger_step_text(t, top)
    assert "11040 W" in text and "25 A" in text and "16 A" in text and "full or cold" in text
    low = {**top, "name": "min", "configured_amps": 6, "amps": 6, "watt": 3000, "attempts": _watts((6, 3000, False), (8, 3000, False), (10, 3000, False))}
    text = charger_step_text(t, low)
    assert "from 6 A to 10 A" in text and "maximum" not in text  # a car that takes less than the lowest step: no maximum to lower
    unproven = {**top, "status": "skip", "reason": "amps_unproven", "watt": 4140}
    assert "4140 W" in charger_step_text(t, unproven)


def test_the_search_stays_in_the_group_and_gives_up_after_a_few() -> None:
    low = {"search": 1, "low": 6, "high": 9}
    assert next_amps(low, _tries((6, False), (8, False))) == 9  # the jump is cut at the top of the group
    assert next_amps(low, _tries((6, False), (8, False), (9, False))) is None
    wide = {"search": 1, "low": 6, "high": 40}
    tries = [(6 + 2 * index, False) for index in range(MAX_ADJUSTMENTS + 1)]
    assert next_amps(wide, _tries(*tries)) is None and next_amps(wide, _tries(*tries[:-1])) is not None


# --- which steps the automation has -----------------------------------------------------------------------------


def _file(tmp_path, automations) -> str:
    import yaml

    path = tmp_path / "automations.yaml"
    path.write_text(yaml.safe_dump(automations), encoding="utf-8")
    return str(path)


def _use(inputs: dict, path: str = "cable_juice_planner/charger_control.yaml") -> dict:
    return {"alias": "charger", "use_blueprint": {"path": path, "input": inputs}}


def test_the_inputs_of_the_automation_of_this_action_sensor_are_found(tmp_path) -> None:
    steps = [{"action": "test.set_current"}]
    file = _file(tmp_path, [
        _use({"action_sensor": ACTION, "amps_actions": steps}),
        _use({"action_sensor": "sensor.cjp_other_charger_action", "stop_actions": steps}),
        _use({"action_sensor": ACTION, "stop_actions": steps}, path="someone/else.yaml"),
        {"alias": "plain", "actions": []},
    ])
    found = automations_using_blueprint(file, ACTION)
    assert found == [{"action_sensor": ACTION, "amps_actions": steps}]
    assert has_steps(found, "amps_actions") is True and has_steps(found, "circuit_actions") is False
    assert has_steps([], "amps_actions") is None  # not found: not known


def test_the_current_start_and_stop_can_be_done_by_several_inputs() -> None:
    steps = [{"action": "test.step"}]
    car = [{"car_start_actions": steps, "car_stop_actions": steps, "car_amps_actions": steps}]
    assert has_steps(car, *AMPS_INPUTS) and has_steps(car, *START_INPUTS) and has_steps(car, *STOP_INPUTS)
    master = [{"circuit_actions": steps}]
    assert has_steps(master, *AMPS_INPUTS) and not has_steps(master, *START_INPUTS)
    pausing = [{"amps_actions": steps}]  # a charger that pauses at 0 A
    assert has_steps(pausing, *START_INPUTS) and has_steps(pausing, *STOP_INPUTS)
    car_amps = [{"car_amps_actions": steps}]  # the car's current is never set to 0
    assert not has_steps(car_amps, *START_INPUTS) and not has_steps(car_amps, *STOP_INPUTS)


def test_an_automation_that_is_not_found_is_not_known(tmp_path) -> None:
    assert automations_using_blueprint(str(tmp_path / "missing.yaml"), ACTION) == []
    broken = tmp_path / "broken.yaml"
    broken.write_text("- [unclosed", encoding="utf-8")
    assert automations_using_blueprint(str(broken), ACTION) == []


# --- the texts ----------------------------------------------------------------------------------------------------


def _texts(language: str) -> dict[str, str]:
    texts: dict[str, str] = {}

    def flatten(data: dict, prefix: str = "") -> None:
        for key, value in data.items():
            if isinstance(value, dict):
                flatten(value, f"{prefix}{key}.")
            else:
                texts[f"{prefix}{key}"] = value

    flatten(json.loads((TEXT_DIR / f"{language}.json").read_text(encoding="utf-8")))
    return texts


STEP_RESULTS = [
    {"name": name, "status": status, "reason": reason, "phases": 3, "configured_amps": 16, "amps": 15, "watt": 3400, "expected_w": 3680,
     "attempts": [{"amps": 16, "watt": 0, "ok": False}, {"amps": 15, "watt": 3400, "ok": True}], "seconds": 80}
    for name in ("max", "min", "single_max", "single_min", "stop")
    for status, reason in (
        ("ok", None), ("adjusted", None), ("fail", None), ("fail", "overdraw"),
        *(("skip", r) for r in SKIP_REASONS),
    )
] + [
    {**STEP_BASE, "status": "adjusted", **extra}
    for STEP_BASE in [
        {"name": name, "reason": None, "phases": 3, "configured_amps": 16, "amps": 12, "watt": 8000, "expected_w": 8280, "attempts": []}
        for name in ("max", "min", "single_max", "single_min")
    ]
    for extra in (
        {"limited": True}, {"already_charging": True}, {"limited": True, "already_charging": True}, {"judged_phases": 1},
        {"voltage": 223.3, "voltage_setting": 240.0}, {"voltage": 223.3, "voltage_setting": 240.0, "judged_phases": 1},
    )
] + [
    {**STEP_BASE, "status": "ok", "voltage": 229.2, "voltage_setting": 240.0}
    for STEP_BASE in [
        {"name": name, "reason": None, "phases": 3, "configured_amps": 16, "amps": 16, "watt": 10967, "expected_w": 11002, "attempts": []}
        for name in ("max", "single_max")
    ]
] + [
    {"name": "limit", "status": status, "reason": reason, "phases": 0, "configured_amps": 0, "amps": 0, "watt": 0, "expected_w": 0,
     "attempts": [], "seconds": 40, "limit_from": 80, "limit_to": 90, "limit_seen": 80}
    for status, reason in (("ok", None), ("fail", None), *(("skip", r) for r in SKIP_REASONS))
]


@pytest.mark.parametrize("language", ["da", "de", "en", "en-GB", "pl", "uk"])
def test_every_text_of_the_result_exists_in_every_language(language) -> None:
    texts = _texts(language)

    def t(key, **fmt):
        assert key in texts, f"{language}: {key}"
        return translate(texts, key, **fmt)

    for status in CHARGER_TEST_STATUSES:
        for reason in (None, *STOP_REASONS):
            for automation, amps_control, car_phases in (("found", True, 1), ("unknown", False, None)):
                for put_back in (None, {"ok": True, "limit": 80}, {"ok": False, "limit": 80}):
                    markdown = build_charger_test_markdown(t, {
                        "status": status, "steps": STEP_RESULTS, "reason": reason, "automation": automation, "amps_control": amps_control,
                        "car_phases": car_phases, "limit_put_back": put_back,
                    })
                    assert "{" not in markdown and "ui.charger_test" not in markdown
        assert "ui.charger_test" not in build_charger_test_markdown(t, {"status": status, "steps": [], "reason": "completed"})
    # a running test: every step is a row from the start (waiting, or tested now), with the progress and the legend
    plan = [{"name": name, "amps": 16, "phases": 3} for name in ("max", "min", "single_max", "single_min", "stop", "limit")]
    for current in (None, "min", "limit"):
        markdown = build_charger_test_markdown(t, {"status": "running", "steps": STEP_RESULTS[:1], "reason": None, "plan": plan, "current": current})
        assert "{" not in markdown and "ui.charger_test" not in markdown
    assert "ui.charger_test" not in build_charger_test_markdown(t, {"status": "idle", "steps": [], "reason": None})
    for key in ("dashboard.section_charger_test", "dashboard.help_charger_test"):
        assert key in texts


def test_the_placeholders_of_a_text_are_the_same_in_every_language() -> None:
    english = {key: value for key, value in _texts("en").items() if key.startswith("ui.charger_test.")}
    assert len(english) > 40
    for language in ("da", "de", "en-GB", "pl", "uk"):
        texts = _texts(language)
        for key, value in english.items():
            assert key in texts, f"{language}: {key}"
            assert sorted(re.findall(r"{(\w+)}", texts[key])) == sorted(re.findall(r"{(\w+)}", value)), f"{language}: {key}"


@pytest.mark.parametrize("language", ["da", "de", "en", "en-GB", "pl", "uk"])
def test_the_dashboard_shows_the_switch_the_status_and_the_result(language) -> None:
    existing = _own("mycar") | {"switch.cjp_mycar_charger_test_run", "sensor.cjp_mycar_charger_test"}
    content = render_dashboard(build_config({**ENGINE_OPTIONS, "language": language}), "mycar", "entry1", existing)
    for entity in ("switch.cjp_mycar_charger_test_run", "sensor.cjp_mycar_charger_test"):
        assert entity in content
    assert content.count("sensor.cjp_mycar_charger_test") >= 2 and "@@" not in content
    assert _texts(language)["dashboard.section_charger_test"] in content


def test_the_markdown_shows_a_row_per_step_and_what_was_tried() -> None:
    texts = _texts("da")
    t = lambda key, **fmt: translate(texts, key, **fmt)  # noqa: E731
    steps = [STEP_RESULTS[0], {**STEP_RESULTS[2], "status": "fail"}, {**STEP_RESULTS[4], "status": "skip", "reason": "no_data"}]
    markdown = build_charger_test_markdown(t, {"status": "done", "steps": steps, "reason": None, "automation": "found", "amps_control": True})
    rows = [line for line in markdown.splitlines() if line.startswith("| ") and "---" not in line][1:]
    assert [row.split(" | ")[0] for row in rows] == ["| ✅", "| ❌", "| 🔧"]
    assert "Prøvet" in rows[1] and "16 A: 0 W" in rows[1]
    assert "1 af 3" in markdown


def _table_rows(markdown: str) -> list[list[str]]:
    rows = [line for line in markdown.splitlines() if line.startswith("| ") and "---" not in line][1:]  # without the header
    return [[cell.strip() for cell in row.strip("|").split("|")] for row in rows]


def test_a_running_test_lists_every_step_from_the_start_with_the_progress() -> None:
    texts = _texts("da")
    t = lambda key, **fmt: translate(texts, key, **fmt)  # noqa: E731
    plan = [
        {"name": "max", "amps": 16, "phases": 3}, {"name": "min", "amps": 6, "phases": 3}, {"name": "stop", "amps": 0, "phases": 0},
        {"name": "limit", "amps": 0, "phases": 0},
    ]
    done = STEP_RESULTS[0]
    assert done["name"] == "max"
    base = {"status": "running", "reason": None, "automation": "found", "amps_control": True, "plan": plan}
    # started: nothing is done, every step waits (the first is not "testing now" until it is measured), with the progress
    start = build_charger_test_markdown(t, {**base, "steps": [], "current": None})
    rows = _table_rows(start)
    assert [row[0] for row in rows] == ["⏳"] * 4
    assert rows[0][1] == "Maksimum: 16 A på 3 fase(r)" and rows[2][1] == "Stop opladning" and rows[3][1] == "Ladegrænse i bilen"
    assert "0 af 4" in start and "▶️" in start.splitlines()[-1] and "⏳" in start.splitlines()[-1]  # the progress and the legend
    # one done, the second is measured now, the rest waits: the stop and the charge limit are last
    mid = build_charger_test_markdown(t, {**base, "steps": [done], "current": "min"})
    rows = _table_rows(mid)
    assert [row[0] for row in rows] == ["✅", "▶️", "⏳", "⏳"]
    assert rows[1][2] == texts["ui.charger_test.testing"] and rows[2][2] == texts["ui.charger_test.waiting"]
    assert "1 af 4" in mid
    # the note on the car's charge limit still sits above the legend while the test runs
    note = build_charger_test_markdown(t, {**base, "steps": [done], "current": None, "limit_put_back": {"ok": None, "limit": 80}})
    assert "80 %" not in note  # ok None: shown only when the test has ended
    # a test that is done (or has no plan, like one restored from before) shows only what has a result, with no waiting rows
    assert [row[0] for row in _table_rows(build_charger_test_markdown(t, {**base, "status": "done", "steps": [done], "current": None}))] == ["✅"]
    old = build_charger_test_markdown(t, {"status": "running", "steps": [done], "reason": None, "automation": "found", "amps_control": True})
    assert [row[0] for row in _table_rows(old)] == ["✅"] and "⏳" not in old


def test_the_markdown_tells_the_measured_voltage_once_per_number_of_phases() -> None:
    texts = _texts("da")
    t = lambda key, **fmt: translate(texts, key, **fmt)  # noqa: E731
    steps = judge_at_measured_voltage(EXPORT_STEPS, 240.0, 5 * 240.0)
    markdown = build_charger_test_markdown(t, {"status": "done", "steps": steps, "reason": None, "automation": "found", "amps_control": True})
    assert "Ret indstillingen" not in markdown
    assert markdown.count("229 V") >= 2 and markdown.count("223 V") >= 2 and markdown.count("240 V") == 2  # in the steps and the notes


# --- the runner, with a charger that follows the published action -----------------------------------------------


@pytest.fixture
def fast_runner():
    base = "custom_components.cable_juice_planner.coordinator.charger_test"
    with (
        patch(f"{base}.ATTEMPT_TIMEOUT", 0.3), patch(f"{base}.SETTLE_SECONDS", 0), patch(f"{base}.SAMPLE_SECONDS", 0.01),
        patch(f"{base}.OVERDRAW_AFTER_SECONDS", 0), patch(f"{base}.LIMIT_TIMEOUT", 0.3), patch(f"{base}.LIMIT_SAMPLE_SECONDS", 0.01),
        patch(f"{base}.LIMIT_PAUSE_SECONDS", 0),
    ):
        yield


def wire_charger(
    hass: HomeAssistant, max_amp: int = 16, min_amp: int = 6, working: bool = True, car_phases: int = 3, cap_w: float | None = None,
    three_phases_always: bool = False, power_at=None, limit_follows=lambda value: True, limits: list | None = None, on_limit=None,
    status_on_stop: str | None = None, status_on_charge: str | None = None,
):
    """The user's automation and charger: the power follows the published action and amps, like a charger would.

    car_phases: the phases the car's on-board charger uses. cap_w: the car takes no more (a full or cold battery).
    three_phases_always: an automation that does not switch the charger to one phase. power_at(amps, phases): the power
    of a charge instead (a charger that does something else). limit_follows(value): whether the car takes a published
    charge limit (the automation's *Set charge limit in the car*); limits gets (action, charge limit) of every publish, and
    on_limit(value) is called when the car took one. status_on_stop: the charger status after a stop that follows a charge
    (an automation that stops the charging in the car: the charger then reports what it reports for a full car).
    status_on_charge: the status at the test's first charge, which then gives no power (a car that is full at once)."""
    seen: list[tuple] = []
    test_charges: list[bool] = []

    def update(_event=None) -> None:
        state = hass.states.get(ACTION)
        if state is None:
            return
        amps = [int(state.attributes.get(f"phase_{index}_amps") or 0) for index in (1, 2, 3)]
        held = [
            getattr(entry.runtime_data.engine, "_charger_hold", None)
            for entry in hass.config_entries.async_entries(DOMAIN) if getattr(entry, "runtime_data", None)
        ]
        if status_on_stop is not None and any(held):
            # only the test's own charge and stop (not the plan's before the test). This runs in a worker thread (a plain
            # function), and the status has listeners in the event loop
            if state.state == "charge":
                test_charges.append(True)
                hass.loop.call_soon_threadsafe(hass.states.async_set, CHARGER_STATUS, "charging")
            elif test_charges:
                hass.loop.call_soon_threadsafe(hass.states.async_set, CHARGER_STATUS, status_on_stop)
        full = status_on_charge is not None and (bool(test_charges) or (state.state == "charge" and any(held)))
        if full and not test_charges:
            test_charges.append(True)
            hass.loop.call_soon_threadsafe(hass.states.async_set, CHARGER_STATUS, status_on_charge)
        seen.append((state.state, tuple(amps)))
        limit = state.attributes.get("charging_limit")
        if limits is not None:
            limits.append((state.state, limit))
        car = hass.states.get(EV_LIMIT)
        # only the limits of the test's limit step (the plan's own, sent with a new charge, is not what is tested here)
        engines = [entry.runtime_data.engine for entry in hass.config_entries.async_entries(DOMAIN) if getattr(entry, "runtime_data", None)]
        testing = any("limit" in (getattr(engine, "_charger_hold", None) or {}) for engine in engines)
        if limit and testing and car is not None and car.state != str(limit) and limit_follows(limit):
            hass.states.async_set(EV_LIMIT, str(limit), dict(car.attributes))
            if on_limit is not None:
                on_limit(limit)
        power = 0.0
        if state.state == "charge" and working and not full:
            per_phase, phases = max(amps), sum(1 for value in amps if value)
            phases = min(3 if three_phases_always else phases, car_phases)
            power = 0.0 if per_phase < min_amp else min(per_phase, max_amp) * phases * VOLTAGE
            power = power if cap_w is None else min(power, cap_w)
            power = power if power_at is None else float(power_at(per_phase, phases))
        hass.states.async_set(CHARGER_POWER, str(power), {"unit_of_measurement": "W", "device_class": "power"})

    async_track_state_change_event(hass, ACTION, update)
    update()
    # the car is not charging when the test begins (else the start is not proven: see the test of that). The entity writes
    # its whole state on the next publish, so the test's first hold is a change the charger sees
    if (state := hass.states.get(ACTION)) is not None:
        hass.states.async_set(ACTION, "stop", dict(state.attributes))
    return seen


@pytest.fixture
async def charger_entry(hass: HomeAssistant):
    """A car on a charger that charges, without solar."""
    set_source_states(hass, charger_status="charging")
    entry = make_entry()
    await async_setup(hass, entry)
    yield entry
    if entry.state is ConfigEntryState.LOADED:
        await async_unload_and_join(hass, entry)


@pytest.fixture
async def solar_entry(hass: HomeAssistant):
    """The same with solar, so the plan steps by amps."""
    set_source_states(hass, charger_status="charging")
    hass.states.async_set("sensor.solar", "0", {"unit_of_measurement": "W", "device_class": "power"})
    entry = make_entry(options={**MOCK_OPTIONS, "production_entity_id": "sensor.solar"})
    await async_setup(hass, entry)
    yield entry
    if entry.state is ConfigEntryState.LOADED:
        await async_unload_and_join(hass, entry)


async def _finished(hass: HomeAssistant) -> None:
    """Wait until the test has ended (the release runs on the worker thread, which the HA waits do not see)."""
    for _ in range(3000):
        await hass.async_block_till_done(wait_background_tasks=True)
        if hass.states.get(RESULT).state != "running":
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the test did not end")


async def _start(hass: HomeAssistant) -> None:
    await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
    await _finished(hass)


def _steps_of(hass: HomeAssistant) -> dict[str, dict]:
    return {step["name"]: step for step in hass.states.get(RESULT).attributes["steps"]}


async def test_without_solar_the_maximum_and_the_stop_are_tried(hass, charger_entry, fast_runner) -> None:
    seen = wire_charger(hass)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    state = hass.states.get(RESULT)
    steps = _steps_of(hass)
    assert state.state == "done" and list(steps) == ["max", "stop", "limit"]
    assert steps["max"]["status"] == "ok" and steps["max"]["watt"] == 16 * 3 * VOLTAGE
    assert steps["stop"]["status"] == "ok"
    assert ("charge", (16, 16, 16)) in seen
    assert hass.states.get(SWITCH).state == "off"
    assert charger_entry.runtime_data.engine._charger_hold is None


async def test_a_running_test_shows_every_step_and_how_far_it_is(hass, charger_entry, fast_runner) -> None:
    coordinator = charger_entry.runtime_data
    seen: list[dict] = []
    publish = coordinator._publish_charger_test

    def watching(**changes) -> None:
        publish(**changes)
        seen.append({key: coordinator.charger_test_state[key] for key in ("status", "steps", "plan", "current", "markdown")})

    wire_charger(hass)
    with patch.object(coordinator, "_publish_charger_test", watching), patch(FOUND, return_value=[]):
        await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
        # published at once, before the engine has said which steps there are
        assert coordinator.charger_test_state["status"] == "running" and coordinator.charger_test_state["steps"] == []
        await _finished(hass)
    running = [item for item in seen if item["status"] == "running"]
    names = ["max", "stop", "limit"]  # without solar: the maximum, the stop and the charge limit
    with_plan = [item for item in running if item["plan"]]
    assert with_plan and all([step["name"] for step in item["plan"]] == names for item in with_plan)
    # the first publication with the plan has every step waiting; the steps then turn into "testing now" one at a time
    assert [row[0] for row in _table_rows(with_plan[0]["markdown"])] == ["⏳"] * 3
    testing = [item["current"] for item in with_plan if item["current"]]
    assert [name for i, name in enumerate(testing) if i == 0 or name != testing[i - 1]] == names
    middle = next(item for item in with_plan if item["current"] == "stop")
    assert [row[0] for row in _table_rows(middle["markdown"])] == ["✅", "▶️", "⏳"]
    # when it is done there is no plan left and every step has its result
    state = hass.states.get(RESULT)
    assert state.state == "done" and state.attributes["plan"] == [] and state.attributes["current"] is None
    assert list(_steps_of(hass)) == names
    assert "⏳" not in state.attributes["markdown"] and "▶️" not in state.attributes["markdown"]


async def test_a_stopped_test_shows_what_was_not_reached_and_no_waiting_rows(hass, solar_entry, fast_runner) -> None:
    wire_charger(hass, working=False)  # a charger that gives no power: the maximum fails and ends the charging steps
    with patch(FOUND, return_value=[]):
        await _start(hass)
    state = hass.states.get(RESULT)
    assert state.state == "stopped" and state.attributes["plan"] == [] and state.attributes["current"] is None
    assert list(_steps_of(hass)) == ["max", "min", "single_max", "single_min", "stop", "limit"]
    assert "⏳" not in state.attributes["markdown"] and "▶️" not in state.attributes["markdown"]


async def test_steps_that_will_not_be_tried_are_not_reached_at_once_not_waiting_during_the_limit_step(hass, solar_entry, fast_runner) -> None:
    coordinator = solar_entry.runtime_data
    seen: list[dict] = []
    publish = coordinator._publish_charger_test

    def watching(**changes) -> None:
        publish(**changes)
        seen.append(dict(coordinator.charger_test_state))

    wire_charger(hass, working=False)  # no power at the maximum: the other charging steps and the stop are not tried
    with patch.object(coordinator, "_publish_charger_test", watching), patch(FOUND, return_value=[]):
        await _start(hass)
    during_limit = next(item for item in seen if item["status"] == "running" and item["current"] == "limit")
    rows = _table_rows(during_limit["markdown"])
    assert [row[0] for row in rows] == ["❌", "🔧", "🔧", "🔧", "🔧", "▶️"]  # nothing waits for a test that will not come
    assert "5 af 6" in during_limit["markdown"]
    assert hass.states.get(RESULT).attributes["plan"] == [] and hass.states.get(RESULT).attributes["current"] is None


async def test_a_stopped_test_does_not_go_on_showing_the_step_that_was_measured(hass, charger_entry, fast_runner) -> None:
    coordinator = charger_entry.runtime_data
    wire_charger(hass)
    with patch(FOUND, return_value=[]), patch("custom_components.cable_juice_planner.coordinator.charger_test.ATTEMPT_TIMEOUT", 30):
        coordinator.async_run_charger_test()
        for _ in range(500):
            await asyncio.sleep(0.01)
            if coordinator.charger_test_state["current"]:
                break
        assert coordinator.charger_test_state["current"] and "▶️" in coordinator.charger_test_state["markdown"]
        coordinator.async_stop_charger_test()
        await _finished(hass)
    state = hass.states.get(RESULT)
    assert state.state == "stopped" and state.attributes["plan"] == [] and state.attributes["current"] is None
    assert "⏳" not in state.attributes["markdown"] and "▶️" not in state.attributes["markdown"]


async def test_the_search_finds_the_current_that_works_at_both_ends(hass, solar_entry, fast_runner) -> None:
    # the car takes nothing below 8 A (the plan's lowest steps are 6 A) and the charger gives 14 A at most
    wire_charger(hass, max_amp=14, min_amp=8)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    steps = _steps_of(hass)
    assert hass.states.get(RESULT).state == "done"
    assert steps["max"]["status"] == "adjusted" and steps["max"]["amps"] == 14 and steps["max"]["configured_amps"] == 16
    assert [a["amps"] for a in steps["max"]["attempts"]] == [16, 14, 15]  # two down, then one back
    assert steps["max"]["limited"]  # 16 A and 15 A gave no more than 14 A: the charger's limit, not the automation
    assert steps["min"]["status"] == "adjusted" and steps["min"]["amps"] == 8 and steps["min"]["configured_amps"] == 6
    assert not steps["min"]["limited"]  # the car took nothing below 8 A: the power rose with the amps
    assert [a["amps"] for a in steps["min"]["attempts"]] == [6, 8, 7]
    assert steps["single_max"]["status"] == "adjusted" and steps["single_max"]["amps"] == 14
    assert steps["single_min"]["status"] == "adjusted" and steps["single_min"]["amps"] == 8
    assert steps["stop"]["status"] == "ok"
    assert "⚠️" in hass.states.get(RESULT).attributes["markdown"]


async def test_a_charger_that_does_not_charge_at_the_maximum_ends_the_test_there(hass, solar_entry, fast_runner) -> None:
    """The maximum is the test that charging works at all: nothing else is tried on a charger that gives no power."""
    limits: list = []
    wire_charger(hass, working=False, limits=limits)
    test = _test_limit(hass)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    state = hass.states.get(RESULT)
    steps = _steps_of(hass)
    assert state.state == "stopped" and state.attributes["reason"] == "no_charging"
    assert steps["max"]["status"] == "fail" and len(steps["max"]["attempts"]) == 1  # no search on a dead charger
    assert all(steps[name]["status"] == "skip" and steps[name]["reason"] == "not_reached" for name in ("min", "single_max", "single_min", "stop"))
    # the charge limit does not depend on the charging: it is still tested, with the charging stopped first
    assert list(steps) == ["max", "min", "single_max", "single_min", "stop", "limit"] and steps["limit"]["status"] == "ok"
    assert {action for action, limit in limits if limit == test} == {"stop"}
    assert solar_entry.runtime_data.engine._charger_hold is None


async def test_a_charger_that_works_but_not_at_the_maximum_is_searched_and_the_steps_go_on(hass, solar_entry, fast_runner) -> None:
    wire_charger(hass, max_amp=10)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    steps = _steps_of(hass)
    assert hass.states.get(RESULT).state == "done"
    assert steps["max"]["status"] == "adjusted" and steps["max"]["amps"] == 10
    assert [a["amps"] for a in steps["max"]["attempts"]] == [16, 14, 12, 10, 11]
    assert steps["max"]["limited"]
    assert steps["min"]["status"] == "ok" and steps["stop"]["status"] == "ok"


async def test_a_lowest_step_that_works_one_amp_nearer_than_the_jump_is_found(hass, solar_entry, fast_runner) -> None:
    wire_charger(hass, min_amp=7)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    steps = _steps_of(hass)
    assert steps["min"]["status"] == "adjusted" and steps["min"]["amps"] == 7
    assert [a["amps"] for a in steps["min"]["attempts"]] == [6, 8, 7]
    assert steps["single_min"]["status"] == "adjusted" and steps["single_min"]["amps"] == 7


async def test_without_steps_for_the_current_only_the_maximum_and_the_stop_are_tried(hass, solar_entry, fast_runner) -> None:
    wire_charger(hass, max_amp=10)  # a charger that ignores the amps would still have to start
    found = [{"start_actions": [{"action": "test.start"}], "stop_actions": [{"action": "test.stop"}]}]
    with patch(FOUND, return_value=found):
        await _start(hass)
    steps = _steps_of(hass)
    assert list(steps) == ["max", "stop", "limit"]
    assert steps["max"]["status"] == "ok" and steps["max"]["expected_w"] == 0  # started: no power to compare with
    assert hass.states.get(RESULT).state == "done"
    assert "Set charging current" in hass.states.get(RESULT).attributes["markdown"]


async def test_a_step_the_automation_has_no_steps_for_is_not_tested(hass, solar_entry, fast_runner) -> None:
    wire_charger(hass)
    found = [{"notify_actions": [{"action": "test.notify"}], "start_actions": [], "stop_actions": []}]
    with patch(FOUND, return_value=found):
        await _start(hass)
    steps = _steps_of(hass)
    assert list(steps) == ["max", "stop", "limit"]
    assert all(step["status"] == "skip" and step["reason"] == "no_steps" for step in steps.values())


async def test_a_charger_that_pauses_at_0_amps_is_tested_with_its_current_alone(hass, solar_entry, fast_runner) -> None:
    wire_charger(hass)
    with patch(FOUND, return_value=[{"amps_actions": [{"action": "test.amps"}], "start_actions": [], "stop_actions": []}]):
        await _start(hass)
    steps = _steps_of(hass)
    assert list(steps) == ["max", "min", "single_max", "single_min", "stop", "limit"]
    assert all(step["status"] in ("ok", "adjusted") for name, step in steps.items() if name != "limit")
    assert steps["limit"]["reason"] == "no_steps"  # no *Set charge limit in the car*


async def test_an_automation_that_does_not_switch_to_one_phase_ends_the_test_at_once(hass, solar_entry, fast_runner) -> None:
    """The load balancing counts on the published amps: a charger taking three phases at "16 A on one phase" is a risk."""
    limits: list = []
    wire_charger(hass, three_phases_always=True, limits=limits)
    test = _test_limit(hass)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    state = hass.states.get(RESULT)
    steps = _steps_of(hass)
    assert state.state == "stopped" and state.attributes["reason"] == "overdraw"
    assert steps["single_max"]["status"] == "fail" and steps["single_max"]["reason"] == "overdraw"
    assert len(steps["single_max"]["attempts"]) == 1  # no search, and never more amps
    assert steps["single_min"]["reason"] == "not_reached" and steps["stop"]["reason"] == "not_reached"
    # the charge limit is still tested, but never while the charger that took too much is held at a charge
    assert steps["limit"]["status"] == "ok"
    assert {action for action, limit in limits if limit == test} == {"stop"}
    assert solar_entry.runtime_data.engine._charger_hold is None


async def test_after_an_overdraw_a_stop_that_is_refused_skips_the_charge_limit(hass, solar_entry, fast_runner) -> None:
    """The engine refuses the stop (it takes one over the test's own hold, so this is the defensive path): the overdrawing
    charge is not kept up for the charge limit, the step is skipped."""
    wire_charger(hass, three_phases_always=True)
    engine = solar_entry.runtime_data.engine
    hold = type(engine).charger_test_hold

    def refuse_stop(self, kind, *args):
        if kind == "stop":
            return {"status": "not_ready", "requested": [0, 0, 0], "published": [0, 0, 0]}
        return hold(self, kind, *args)

    with patch(FOUND, return_value=[]), patch.object(type(engine), "charger_test_hold", refuse_stop):
        await _start(hass)
    steps = _steps_of(hass)
    assert hass.states.get(RESULT).attributes["reason"] == "overdraw"
    assert steps["limit"]["status"] == "skip" and steps["limit"]["reason"] == "not_ready"
    assert engine._charger_hold is None


async def test_a_car_that_stops_itself_on_the_stop_passes_it_and_the_charge_limit_is_tested(hass, charger_entry, fast_runner) -> None:
    """The automation stops the charging in the car: the charger then says completed, like for a full car. After CJP's
    own stop that is what a stop looks like, and the power shows it."""
    wire_charger(hass, status_on_stop="completed")
    with patch(FOUND, return_value=[]):
        await _start(hass)
    state = hass.states.get(RESULT)
    steps = _steps_of(hass)
    assert state.state == "done" and state.attributes["reason"] is None, steps
    assert steps["stop"]["status"] == "ok" and steps["limit"]["status"] == "ok"


async def test_a_car_that_is_full_during_a_charge_step_has_its_limit_tested_at_a_stop(hass, charger_entry, fast_runner) -> None:
    """The charger reports completed during the charging: the held charge is replaced by a stop before the limit step (the
    engine takes a stop over the test's own hold whatever the status), so the car can't resume on the test's charge."""
    limits: list = []
    wire_charger(hass, status_on_charge="completed", limits=limits)
    test = _test_limit(hass)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    state = hass.states.get(RESULT)
    steps = _steps_of(hass)
    assert state.state == "stopped" and state.attributes["reason"] == "completed", steps
    assert steps["max"]["status"] == "skip" and steps["stop"]["reason"] == "not_reached"
    assert steps["limit"]["status"] == "ok"
    assert {action for action, limit in limits if limit == test} == {"stop"}
    assert charger_entry.runtime_data.engine._charger_hold is None


async def test_a_car_unplugged_after_the_stop_does_not_prove_the_stop(hass, charger_entry, fast_runner) -> None:
    wire_charger(hass, status_on_stop="disconnected")
    with patch(FOUND, return_value=[]):
        await _start(hass)
    state = hass.states.get(RESULT)
    steps = _steps_of(hass)
    assert state.state == "stopped" and state.attributes["reason"] == "not_connected"
    assert steps["stop"]["status"] == "skip" and steps["stop"]["reason"] == "not_ready"
    assert steps["limit"]["status"] == "ok"  # the car's limit is set in the car, not through the cable


async def test_a_grid_voltage_below_the_setting_is_measured_not_told_as_other_amps(hass, solar_entry, fast_runner) -> None:
    """The power follows every current, at 93 % of amps x the voltage setting: the voltage is wrong, not the automation."""
    wire_charger(hass, power_at=lambda amps, phases: amps * phases * VOLTAGE * 0.93)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    state = hass.states.get(RESULT)
    steps = _steps_of(hass)
    assert state.state == "done", steps
    for name in ("max", "min", "single_max", "single_min"):
        assert steps[name]["status"] == "ok" and steps[name]["amps"] == steps[name]["configured_amps"], steps[name]
        assert steps[name]["voltage"] == pytest.approx(VOLTAGE * 0.93, abs=0.5)
    assert steps["stop"]["status"] == "ok"  # the stop is tried at a current that worked
    assert "Ret indstillingen" not in state.attributes["markdown"] and "214 V" in state.attributes["markdown"]


async def test_a_voltage_the_later_steps_do_not_confirm_is_not_kept(hass, solar_entry, fast_runner) -> None:
    """The 1-phase maximum alone gives one voltage, the 1-phase minimum another: the steps are judged from what was
    measured again, so the maximum is not left at a voltage the minimum disproves."""
    def power(amps, phases):
        return amps * phases * VOLTAGE * (0.93 if phases == 1 and amps > 6 else 1.0)

    wire_charger(hass, power_at=power)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    steps = _steps_of(hass)
    assert steps["single_min"]["status"] == "ok" and "voltage" not in steps["single_min"]
    assert steps["single_max"]["status"] == "adjusted" and steps["single_max"]["amps"] < 16 and "voltage" not in steps["single_max"], steps["single_max"]
    assert steps["max"]["status"] == "ok" and "voltage" not in steps["max"]


async def test_a_car_that_already_charges_marks_the_first_step_that_works_at_the_measured_voltage(hass, solar_entry, fast_runner) -> None:
    """The maximum fails at the voltage setting and works at the measured voltage: it is the first step that charged, so it
    is the one that does not prove the start (not the 1-phase step that happened to pass at the setting)."""
    wire_charger(hass, power_at=lambda amps, phases: amps * phases * VOLTAGE * 0.93)
    await hass.async_block_till_done()
    hass.states.async_set(CHARGER_POWER, "7000", {"unit_of_measurement": "W", "device_class": "power"})
    with patch(FOUND, return_value=[]):
        await _start(hass)
    steps = _steps_of(hass)
    assert steps["max"]["status"] == "adjusted" and steps["max"]["already_charging"] and steps["max"]["voltage"], steps["max"]
    assert [name for name, step in steps.items() if step.get("already_charging")] == ["max"]
    assert steps["single_max"]["status"] == "ok"


async def test_a_charger_that_always_gives_an_amp_less_is_not_explained_by_the_voltage(hass, solar_entry, fast_runner) -> None:
    wire_charger(hass, power_at=lambda amps, phases: (amps - 1) * phases * VOLTAGE)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    steps = _steps_of(hass)
    assert steps["max"]["status"] == "fail" and steps["min"]["status"] != "ok"
    assert not any(step.get("voltage") for step in steps.values())


async def test_a_car_that_charges_on_one_phase_is_judged_on_one_phase(hass, solar_entry, fast_runner) -> None:
    wire_charger(hass, car_phases=1)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    state = hass.states.get(RESULT)
    steps = _steps_of(hass)
    assert state.state == "done" and state.attributes["car_phases"] == 1 and state.attributes["reason"] is None
    assert steps["max"]["status"] == "ok" and steps["max"]["judged_phases"] == 1 and steps["max"]["expected_w"] == 16 * VOLTAGE
    assert steps["min"]["status"] == "ok" and steps["stop"]["status"] == "ok"
    assert "1 fase" in state.attributes["markdown"]


async def test_a_car_that_limits_the_power_is_not_told_to_change_the_setting(hass, solar_entry, fast_runner) -> None:
    wire_charger(hass, cap_w=8000.0)  # a full battery: the car takes 8 kW whatever the amps
    with patch(FOUND, return_value=[]):
        await _start(hass)
    state = hass.states.get(RESULT)
    steps = _steps_of(hass)
    assert state.state == "done"
    assert steps["max"]["status"] == "adjusted" and steps["max"]["limited"] and steps["max"]["amps"] == 12
    assert "8000 W" in state.attributes["markdown"] and "Ret indstillingen" not in state.attributes["markdown"]
    assert steps["min"]["status"] == "ok" and steps["stop"]["status"] == "ok"


async def test_a_car_that_limits_the_power_where_fewer_phases_would_be_is_not_one_on_fewer_phases(hass, solar_entry, fast_runner) -> None:
    """7360 W is 16 A on two phases, but the power stays there at lower currents too: the car's limit, not two phases."""
    wire_charger(hass, cap_w=16 * 2 * VOLTAGE)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    state = hass.states.get(RESULT)
    steps = _steps_of(hass)
    assert state.state == "done" and state.attributes["car_phases"] is None
    assert steps["max"]["status"] == "adjusted" and steps["max"]["limited"] and not steps["max"].get("judged_phases")
    assert steps["min"]["status"] == "ok"  # judged on three phases, as the car charges


@pytest.fixture
async def big_charger_entry(hass: HomeAssistant):
    """A 25 A charger with solar."""
    set_source_states(hass, charger_status="charging")
    hass.states.async_set("sensor.solar", "0", {"unit_of_measurement": "W", "device_class": "power"})
    entry = make_entry(options={**MOCK_OPTIONS, "production_entity_id": "sensor.solar", "charging_max_amp": 25.0})
    await async_setup(hass, entry)
    yield entry
    if entry.state is ConfigEntryState.LOADED:
        await async_unload_and_join(hass, entry)


async def test_an_11_kw_car_on_a_bigger_charger_is_told_the_current_it_takes(hass, big_charger_entry, fast_runner) -> None:
    """11040 W matches 25 A and 23 A on two phases within the tolerance, at the same power: not two phases."""
    wire_charger(hass, max_amp=25, cap_w=16 * 3 * VOLTAGE)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    state = hass.states.get(RESULT)
    steps = _steps_of(hass)
    assert state.attributes["car_phases"] is None
    assert steps["max"]["status"] == "adjusted" and steps["max"]["limited"] and steps["max"]["amps"] == 16
    assert steps["min"]["status"] == "ok"
    assert "16 A" in state.attributes["markdown"] and "25 A" in state.attributes["markdown"]


async def test_a_car_on_two_phases_is_found_on_two_currents(hass, solar_entry, fast_runner) -> None:
    wire_charger(hass, car_phases=2)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    state = hass.states.get(RESULT)
    steps = _steps_of(hass)
    assert state.attributes["car_phases"] == 2
    assert steps["max"]["status"] == "ok" and steps["max"]["amps"] == 16 and steps["max"]["judged_phases"] == 2
    assert [attempt["amps"] for attempt in steps["max"]["attempts"]] == [16, 14]  # the second current confirms the phases
    assert steps["min"]["status"] == "ok"


@pytest.fixture
async def three_phase_solar_entry(hass: HomeAssistant):
    """Solar without 1-phase steps: only the maximum and the lowest step on three phases."""
    set_source_states(hass, charger_status="charging")
    hass.states.async_set("sensor.solar", "0", {"unit_of_measurement": "W", "device_class": "power"})
    entry = make_entry(options={**MOCK_OPTIONS, "production_entity_id": "sensor.solar", "charging_single_phase_max_amp": 0.0})
    await async_setup(hass, entry)
    yield entry
    if entry.state is ConfigEntryState.LOADED:
        await async_unload_and_join(hass, entry)


async def test_a_charger_stuck_at_one_current_is_not_counted_as_working(hass, three_phase_solar_entry, fast_runner) -> None:
    """An automation that never sets the current: the charger stays at 6 A, which happens to be near the lowest step."""
    wire_charger(hass, power_at=lambda amps, phases: 6 * 3 * VOLTAGE)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    state = hass.states.get(RESULT)
    steps = _steps_of(hass)
    assert list(steps) == ["max", "min", "stop", "limit"]
    assert steps["limit"]["status"] == "ok"  # not a current: amps_unproven leaves it
    assert all(steps[name]["status"] == "skip" and steps[name]["reason"] == "amps_unproven" for name in ("max", "min"))
    assert steps["stop"]["status"] == "ok"  # the charger does stop: that is proven
    assert "2 af 4" in state.attributes["markdown"] and "4140 W" in state.attributes["markdown"]


async def test_a_charger_that_switches_the_phases_but_not_the_current_is_not_counted_as_working(hass, solar_entry, fast_runner) -> None:
    wire_charger(hass, power_at=lambda amps, phases: 6 * phases * VOLTAGE)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    steps = _steps_of(hass)
    assert all(steps[name]["reason"] == "amps_unproven" for name in ("max", "min", "single_max", "single_min")), steps
    assert steps["stop"]["status"] == "ok"


async def test_a_car_on_fewer_phases_is_confirmed_below_a_maximum_that_is_the_only_step(hass, charger_entry, fast_runner) -> None:
    """Without solar steps the maximum is the only current of its step: one lower current confirms the phases."""
    wire_charger(hass, car_phases=1)
    found = [{"amps_actions": [{"action": "test.amps"}], "start_actions": [{"action": "test.start"}], "stop_actions": [{"action": "test.stop"}]}]
    with patch(FOUND, return_value=found):
        await _start(hass)
    state = hass.states.get(RESULT)
    steps = _steps_of(hass)
    assert list(steps) == ["max", "stop", "limit"] and state.attributes["car_phases"] == 1
    assert steps["max"]["status"] == "ok" and steps["max"]["amps"] == 16 and steps["max"]["judged_phases"] == 1
    assert [attempt["amps"] for attempt in steps["max"]["attempts"]] == [16, 14]
    assert steps["stop"]["status"] == "ok"


async def test_a_maximum_that_charged_once_is_not_a_charger_that_does_not_charge(hass, solar_entry, fast_runner) -> None:
    """The first current gave power (another than asked for), the next nothing: the test goes on."""
    wire_charger(hass, power_at=lambda amps, phases: 5000.0 if amps == 16 else 0.0)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    state = hass.states.get(RESULT)
    steps = _steps_of(hass)
    assert state.attributes["reason"] != "no_charging"
    assert steps["max"]["status"] == "fail" and len(steps["max"]["attempts"]) > 2
    assert steps["min"]["reason"] != "not_reached"


async def test_a_car_that_already_charges_does_not_prove_the_start(hass, charger_entry, fast_runner) -> None:
    wire_charger(hass)
    await hass.async_block_till_done()  # the charger has seen the stop of wire_charger
    hass.states.async_set(CHARGER_POWER, "7000", {"unit_of_measurement": "W", "device_class": "power"})
    with patch(FOUND, return_value=[]):
        await _start(hass)
    steps = _steps_of(hass)
    assert steps["max"]["status"] == "adjusted" and steps["max"]["already_charging"]
    assert "starten er ikke bevist" in hass.states.get(RESULT).attributes["markdown"]
    assert not steps["stop"].get("already_charging")


async def test_a_limit_of_the_load_balancing_during_a_step_is_not_judged(hass, charger_entry, fast_runner) -> None:
    wire_charger(hass)
    engine = charger_entry.runtime_data.engine
    with patch(FOUND, return_value=[]), patch.object(type(engine), "charger_test_published", lambda self: [6, 6, 6]):
        await _start(hass)
    steps = _steps_of(hass)
    assert steps["max"]["status"] == "skip" and steps["max"]["reason"] == "site_limited"


async def test_a_car_that_is_unplugged_during_the_test_ends_it(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    wire_charger(hass, working=False)  # the step waits for power that does not come
    with patch("custom_components.cable_juice_planner.coordinator.charger_test.SAMPLE_SECONDS", 0.01), patch(FOUND, return_value=[]):
        coordinator.async_run_charger_test()
        for _ in range(500):
            await asyncio.sleep(0.01)
            if coordinator.engine._charger_hold is not None:
                break
        assert coordinator.engine._charger_hold is not None
        hass.states.async_set(CHARGER_STATUS, "disconnected")
        await _finished(hass)
    state = hass.states.get(RESULT)
    assert state.state == "stopped" and state.attributes["reason"] == "not_connected"
    assert _steps_of(hass)["stop"]["reason"] == "not_reached"
    assert _steps_of(hass)["limit"]["status"] == "ok"  # the car's limit is set in the car, not through the cable
    assert coordinator.engine._charger_hold is None


@pytest.mark.parametrize(("status", "reason"), [("disconnected", "not_connected"), ("completed", "completed"), ("unavailable", "unavailable")])
async def test_a_test_does_not_start_without_a_car_that_can_charge(hass, charger_entry, fast_runner, status, reason) -> None:
    hass.states.async_set(CHARGER_STATUS, status)
    await _start(hass)
    state = hass.states.get(RESULT)
    assert state.state == "failed" and state.attributes["reason"] == reason and state.attributes["steps"] == []
    assert charger_entry.runtime_data.engine._charger_hold is None


async def test_a_test_needs_the_power_sensor(hass, fast_runner) -> None:
    set_source_states(hass, charger_status="charging")
    entry = make_entry(options={**MOCK_OPTIONS, "charger_power_entity_id": ""})
    await async_setup(hass, entry)
    await _start(hass)
    assert hass.states.get(RESULT).attributes["reason"] == "not_configured"
    await async_unload_and_join(hass, entry)


async def test_a_step_the_load_balancing_limits_is_not_pushed_past_it(hass, charger_entry, fast_runner) -> None:
    wire_charger(hass)
    engine = charger_entry.runtime_data.engine
    original = type(engine).charger_test_hold

    def limited(self, kind, amps, phases, seconds):
        out = original(self, kind, amps, phases, seconds)
        if kind == "charge":
            out["published"] = [min(a, 6) for a in out["requested"]]
        return out

    with patch(FOUND, return_value=[]), patch.object(type(engine), "charger_test_hold", limited):
        await _start(hass)
    steps = _steps_of(hass)
    assert steps["max"]["status"] == "skip" and steps["max"]["reason"] == "site_limited"


async def test_the_held_current_survives_the_cron_and_the_plans_stop(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    held = (await coordinator.async_run_job("charger_test_hold", "charge", 10, 3, 600)).value
    assert held["status"] == "ok" and held["published"] == [10, 10, 10]
    for job in ("charge_trigger", "recalculate"):
        await coordinator.async_run_job(job, include_forecast=True)
        state = hass.states.get(ACTION)
        assert state.state == "charge" and [state.attributes[f"phase_{i}_amps"] for i in (1, 2, 3)] == [10, 10, 10]
    engine = coordinator.engine
    await coordinator.async_run_job("stop_charging", None, True)  # what "charging without a rule" does: the test is not stopped by it
    assert engine.charger_action == "charge" and engine._charger_hold is not None
    await coordinator.async_run_job("charger_test_release", include_forecast=True)
    assert engine._charger_hold is None


async def test_a_hold_that_ran_out_is_dropped_by_the_next_calculation(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    await coordinator.async_run_job("charge_trigger", include_forecast=True)
    before = _published(hass)
    await coordinator.async_run_job("charger_test_hold", "charge", 10, 3, 600)
    coordinator.engine._charger_hold["until"] = coordinator.engine._charger_hold["since"]  # it ended
    await coordinator.async_run_job("charge_trigger", include_forecast=True)
    assert coordinator.engine._charger_hold is None and coordinator.engine._charger_abort == "expired"
    assert coordinator.engine._charger_saved is None
    assert _published(hass) == before and before[2] != (10, 10, 10)


RULE = "sensor.cjp_test_current_charging_rule"


async def test_the_rule_says_that_the_test_runs_and_what_it_publishes(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    engine = coordinator.engine
    await coordinator.async_run_job("charge_trigger", include_forecast=True)
    assert "🧪" not in hass.states.get(RULE).state
    await coordinator.async_run_job("charger_test_hold", "charge", 10, 3, 600)
    charging = hass.states.get(RULE).state
    assert charging.startswith("🧪") and engine.t("ui.charger_test.rule_charge", amps=10, phases=3) in charging, charging
    await coordinator.async_run_job("charge_trigger", include_forecast=True)  # the cron while held keeps it
    assert hass.states.get(RULE).state == charging
    await coordinator.async_run_job("charger_test_hold", "stop", 0, 0, 600)
    stopping = hass.states.get(RULE).state
    assert stopping.startswith("🧪") and engine.t("ui.charger_test.step.stop") in stopping, stopping
    await coordinator.async_run_job("charger_test_release", include_forecast=True)
    assert "🧪" not in hass.states.get(RULE).state  # the plan's own rule again


async def test_a_hold_that_ran_out_outside_a_calculation_puts_the_plans_rule_back(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    engine = coordinator.engine
    with _site(engine, [16, 16, 16]):
        await coordinator.async_run_job("charge_trigger", include_forecast=True)
        before = hass.states.get(RULE).state
        await coordinator.async_run_job("charger_test_hold", "charge", 10, 3, 600)
        assert hass.states.get(RULE).state.startswith("🧪")
        await coordinator.async_run_job("charger_test_hold", "stop", 0, 0, 600)  # a second hold keeps the plan's rule
        engine._charger_hold["until"] = engine._charger_hold["since"]
        await coordinator.async_run_job("site_limit")  # the site's job finds that it ran out
        assert engine._charger_hold is None and hass.states.get(RULE).state == before


async def test_a_hold_after_a_test_rule_was_left_never_brings_the_test_rule_back(hass, charger_entry) -> None:
    """A hold that ran out before the plan had set its own rule again: the next test must not save the old 🧪 text."""
    coordinator = charger_entry.runtime_data
    engine = coordinator.engine
    await coordinator.async_run_job("charger_test_hold", "charge", 10, 3, 600)
    engine._charger_saved["rule"] = ""  # no rule of the plan before the test
    engine._charger_hold["until"] = engine._charger_hold["since"]
    await coordinator.async_run_job("charger_test_published")  # a job that only asks finds that it ran out
    assert engine._charger_hold is None and not hass.states.get(RULE).state.startswith("🧪")
    engine.last_charging_rule_text = "🧪left over"  # as if the restore had not set a rule
    await coordinator.async_run_job("charger_test_hold", "charge", 10, 3, 600)
    engine._charger_hold["until"] = engine._charger_hold["since"]
    await coordinator.async_run_job("charger_test_published")
    assert not hass.states.get(RULE).state.startswith("🧪")


async def test_the_rule_of_the_limit_step_shows_the_published_charge_limit(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    engine = coordinator.engine
    await coordinator.async_run_job("charger_test_hold", "stop", 0, 0, 600)
    await coordinator.async_run_job("charger_test_limit", 90.0, 80.0, 600)
    rule = hass.states.get(RULE).state
    assert rule.startswith("🧪") and engine.t("ui.charger_test.step.stop") in rule and "90 %" in rule, rule
    await coordinator.async_run_job("charger_test_limit", 80.0, 80.0, 600)  # the car's own limit put back
    assert "80 %" in hass.states.get(RULE).state
    await coordinator.async_run_job("charger_test_release", include_forecast=True)
    assert "🧪" not in hass.states.get(RULE).state


async def test_other_jobs_do_not_replace_the_rule_while_a_test_holds_the_charger(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    await coordinator.async_run_job("charger_test_hold", "charge", 10, 3, 600)
    held = hass.states.get(RULE).state
    await coordinator.async_run_job("set_charging_rule", "⚠️another job")  # e.g. a notification checking the car's port
    assert hass.states.get(RULE).state == held
    await coordinator.async_run_job("charger_test_release", include_forecast=True)
    assert "🧪" not in hass.states.get(RULE).state


async def test_a_text_after_a_hold_ran_out_wins_although_nothing_had_noticed_it_yet(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    engine = coordinator.engine
    await coordinator.async_run_job("charger_test_hold", "charge", 10, 3, 600)
    engine._charger_hold["until"] = engine._charger_hold["since"]  # ran out, not noticed by any job yet
    await coordinator.async_run_job("set_charging_rule", "⚠️cable out")
    assert engine._charger_hold is None and hass.states.get(RULE).state.startswith("⚠️cable out")


@pytest.mark.parametrize("gave_up", [False, True])
async def test_a_restart_the_test_ended_does_not_come_back_but_a_give_up_does(hass, charger_entry, gave_up) -> None:
    """A restart in progress is ended by the test, so its text is obsolete; a give-up is kept, and so is its text."""
    coordinator = charger_entry.runtime_data
    engine = coordinator.engine
    restart_rule = "⛔restart text"
    engine._charger_restart_rule = restart_rule
    engine.restarting_charger = not gave_up
    engine._charger_restart_gave_up = gave_up
    await coordinator.async_run_job("set_charging_rule", restart_rule)
    await coordinator.async_run_job("charger_test_hold", "charge", 10, 3, 600)
    engine._charger_hold["until"] = engine._charger_hold["since"]
    await coordinator.async_run_job("charger_test_published")  # a job that only asks finds that it ran out
    rule = hass.states.get(RULE).state
    if gave_up:
        assert rule.startswith(restart_rule), rule
    else:
        assert rule.startswith(engine.t("ui.charger_test.rule_ended")), rule


async def test_a_hold_that_ran_out_found_by_the_cron_shows_the_plans_rule(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    engine = coordinator.engine
    await coordinator.async_run_job("charger_test_hold", "charge", 10, 3, 600)
    engine._charger_hold["until"] = engine._charger_hold["since"]
    await coordinator.async_run_job("charge_trigger", include_forecast=True)
    assert engine._charger_hold is None and not hass.states.get(RULE).state.startswith("🧪")


async def test_a_held_charge_the_site_stops_shows_a_stop_in_the_rule(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    engine = coordinator.engine
    stop = engine.t("ui.charger_test.step.stop")
    with _site(engine, [16, 16, 16]) as site:
        await coordinator.async_run_job("charger_test_hold", "charge", 10, 3, 600)
        assert stop not in hass.states.get(RULE).state
        site["live"] = [0, 0, 0]
        await coordinator.async_run_job("site_limit")
        assert hass.states.get(RULE).state.startswith("🧪") and stop in hass.states.get(RULE).state
        site["live"] = [16, 16, 16]
        await coordinator.async_run_job("site_limit")
        assert stop not in hass.states.get(RULE).state
        await coordinator.async_run_job("charger_test_release", include_forecast=True)


@contextmanager
def _site(engine, live: list[int]):
    """A site whose live capacity for this car is live (amps per phase), changed by the test; the real clamp is used."""
    site = {"live": list(live), "hour_capacity": {}, "urgent_hours": 12.0}
    with patch.object(type(engine), "site_active", lambda self: True), patch.object(type(engine), "_site", lambda self: site):
        yield site


async def test_a_held_charge_the_site_has_no_current_for_is_published_as_stop(hass, charger_entry) -> None:
    """A blueprint starts the charger on "charge" also with 0 A: the site's stop must not be lost while a test holds a current."""
    coordinator = charger_entry.runtime_data
    engine = coordinator.engine
    with _site(engine, [16, 16, 16]) as site:
        held = (await coordinator.async_run_job("charger_test_hold", "charge", 10, 3, 600)).value
        assert held["published"] == [10, 10, 10] and _published(hass) == ("charge", True, (10, 10, 10))
        site["live"] = [0, 0, 0]  # another car or the house takes it all: the site's own job applies the limit
        await coordinator.async_run_job("site_limit")
        assert _published(hass) == ("stop", True, (0, 0, 0))
        await coordinator.async_run_job("charge_trigger", include_forecast=True)  # the cron while held: still a stop
        assert _published(hass) == ("stop", True, (0, 0, 0))
        site["live"] = [16, 16, 16]
        await coordinator.async_run_job("site_limit")
        assert _published(hass) == ("charge", True, (10, 10, 10))
        held = (await coordinator.async_run_job("charger_test_hold", "charge", 16, 3, 600)).value
        site["live"] = [0, 0, 0]
        held = (await coordinator.async_run_job("charger_test_hold", "charge", 16, 3, 600)).value
        assert held["published"] == [0, 0, 0] and _published(hass) == ("stop", True, (0, 0, 0))  # also on the hold itself
        await coordinator.async_run_job("charger_test_release", include_forecast=True)


async def test_a_site_stop_of_a_hold_ends_when_the_site_is_gone(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    engine = coordinator.engine
    with _site(engine, [0, 0, 0]):
        await coordinator.async_run_job("charger_test_hold", "charge", 10, 3, 600)
        assert _published(hass)[0] == "stop"
    await coordinator.async_run_job("charge_trigger", include_forecast=True)  # the site entry was unloaded
    assert _published(hass) == ("charge", True, (10, 10, 10))
    await coordinator.async_run_job("charger_test_release", include_forecast=True)


async def test_the_release_after_a_plan_the_site_had_stopped_puts_back_the_stop_without_amps(hass, charger_entry) -> None:
    """The plan asked for 16 A, the site gave none: the stop must not come back with the 16 A the site has later."""
    coordinator = charger_entry.runtime_data
    engine = coordinator.engine
    with _site(engine, [0, 0, 0]) as site:
        await coordinator.async_run_job("start_charging")
        await coordinator.async_run_job("set_charger_phase_limits", [16, 16, 16], [16, 16, 16])
        assert _published(hass)[0] == "stop" and _published(hass)[2] == (0, 0, 0)
        site["live"] = [16, 16, 16]
        await coordinator.async_run_job("charger_test_hold", "charge", 10, 3, 600)
        with patch.object(type(engine), "other_ev_connected", return_value=True):
            await coordinator.async_run_job("charger_test_release", include_forecast=True)
        assert _published(hass)[0] == "stop" and _published(hass)[2] == (0, 0, 0)


async def test_the_release_does_not_put_back_requested_amps_the_site_never_saw(hass, charger_entry) -> None:
    """The site came after the plan's last run: requested_* are still zeros, the published amps are the plan's."""
    coordinator = charger_entry.runtime_data
    engine = coordinator.engine
    await coordinator.async_run_job("start_charging")
    await coordinator.async_run_job("set_charger_phase_limits", [16, 16, 16], [16, 16, 16])  # no site yet
    engine.requested_charger_amps, engine.requested_circuit_amps = [0, 0, 0], [0, 0, 0]
    with _site(engine, [16, 16, 16]):
        await coordinator.async_run_job("charger_test_hold", "charge", 10, 3, 600)
        with patch.object(type(engine), "other_ev_connected", return_value=True):
            await coordinator.async_run_job("charger_test_release", include_forecast=True)
        assert _published(hass) == ("charge", True, (16, 16, 16))


async def test_a_test_with_a_site_that_has_no_current_never_publishes_charge_without_amps(hass, charger_entry, fast_runner) -> None:
    seen = wire_charger(hass)
    engine = charger_entry.runtime_data.engine
    with _site(engine, [0, 0, 0]), patch(FOUND, return_value=[]):
        start = len(seen)
        await _start(hass)
    steps = _steps_of(hass)
    assert steps["max"]["status"] == "skip" and steps["max"]["reason"] == "site_limited"
    assert steps["stop"]["reason"] == "no_baseline"
    assert ("charge", (0, 0, 0)) not in seen[start:]


async def test_the_wait_ends_at_once_when_the_site_limits_the_published_amps(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    hass.states.async_set(CHARGER_POWER, "11040", {"unit_of_measurement": "W"})
    coordinator.attributes["charger_action"] = {**(coordinator.attributes.get("charger_action") or {}), "phase_1_amps": 6, "phase_2_amps": 6, "phase_3_amps": 6}
    assert coordinator._published_amps() == [6, 6, 6]  # read at once: a calculation of the plan may publish its own later
    with (
        patch("custom_components.cable_juice_planner.coordinator.charger_test.SAMPLE_SECONDS", 0.01),
        patch("custom_components.cable_juice_planner.coordinator.charger_test.SETTLE_SECONDS", 0),
        patch.object(coordinator, "_published_amps", return_value=[6, 6, 6]),
    ):
        verdict, _mean, _seconds = await coordinator._async_wait_for_power(CHARGER_POWER, lambda value: True, None, [16, 16, 16])
        assert verdict == "limited"
        verdict, _mean, _seconds = await coordinator._async_wait_for_power(CHARGER_POWER, lambda value: True, None, [6, 6, 6])
        assert verdict != "limited"


async def test_power_above_the_overdraw_never_passes_a_check_that_is_a_lower_bound(hass, charger_entry) -> None:
    """With the real order of the times (settle, the first possible pass, the overdraw): a start or the stop step's charging
    that takes clearly more than it was given is an overdraw, not a pass."""
    coordinator = charger_entry.runtime_data
    hass.states.async_set(CHARGER_POWER, "20000", {"unit_of_measurement": "W"})
    base = "custom_components.cable_juice_planner.coordinator.charger_test"
    with (
        patch(f"{base}.ATTEMPT_TIMEOUT", 3.0), patch(f"{base}.SETTLE_SECONDS", 0.1), patch(f"{base}.SAMPLE_SECONDS", 0.02),
        patch(f"{base}.OVERDRAW_AFTER_SECONDS", 0.5),
    ):
        verdict, mean, seconds = await coordinator._async_wait_for_power(CHARGER_POWER, lambda value: value >= 1000, 5000.0)
        assert verdict == "over" and mean == 20000.0
        verdict, _mean, _seconds = await coordinator._async_wait_for_power(CHARGER_POWER, lambda value: value >= 1000, 25000.0)
        assert verdict == "ok"  # under the overdraw it passes as before


async def test_a_hold_that_ran_out_while_the_site_applies_its_limit_puts_back_the_plans_amps(hass, charger_entry) -> None:
    """The site's job read the test's amps before the hold was dropped: it must publish what was there before the test."""
    coordinator = charger_entry.runtime_data
    engine = coordinator.engine
    with _site(engine, [16, 16, 16]):
        await coordinator.async_run_job("start_charging")
        await coordinator.async_run_job("set_charger_phase_limits", [16, 16, 16], [16, 16, 16])
        before = _published(hass)
        await coordinator.async_run_job("charger_test_hold", "charge", 10, 3, 600)
        engine._charger_hold["until"] = engine._charger_hold["since"]
        await coordinator.async_run_job("site_limit")
        assert engine._charger_hold is None and _published(hass) == before == ("charge", True, (16, 16, 16))


async def test_the_release_puts_back_what_the_plan_asked_for_not_what_the_site_allowed_then(hass, charger_entry) -> None:
    """With another car on the charger the plan returns early: the site's job must find the plan's amps, not the clamped ones."""
    coordinator = charger_entry.runtime_data
    engine = coordinator.engine
    with _site(engine, [6, 6, 6]) as site:
        await coordinator.async_run_job("start_charging")
        await coordinator.async_run_job("set_charger_phase_limits", [16, 16, 16], [16, 16, 16])
        assert engine.current_charging_amps == [6, 6, 6]
        await coordinator.async_run_job("charger_test_hold", "charge", 10, 3, 600)
        with patch.object(type(engine), "other_ev_connected", return_value=True):
            await coordinator.async_run_job("charger_test_release", include_forecast=True)
            site["live"] = [16, 16, 16]
            await coordinator.async_run_job("site_limit")
        assert engine.requested_charger_amps == [16, 16, 16] and engine.current_charging_amps == [16, 16, 16]


def _published(hass: HomeAssistant) -> tuple:
    state = hass.states.get(ACTION)
    return state.state, state.attributes["charger_enabled"], tuple(state.attributes[f"phase_{i}_amps"] for i in (1, 2, 3))


async def test_the_release_puts_back_what_was_published_before_also_when_the_plan_returns_early(hass, charger_entry) -> None:
    """With another car on the charger the plan publishes nothing: the test's own values must not stay."""
    coordinator = charger_entry.runtime_data
    engine = coordinator.engine
    await coordinator.async_run_job("charge_trigger", include_forecast=True)
    before = _published(hass)
    for kind, amps in (("charge", 10), ("stop", 0), ("charge", 7)):  # the first hold is what is put back
        await coordinator.async_run_job("charger_test_hold", kind, amps, 3, 600)
    assert _published(hass)[0] == "charge" and _published(hass)[2] == (7, 7, 7)
    with patch.object(type(engine), "other_ev_connected", return_value=True):
        await coordinator.async_run_job("charger_test_release", include_forecast=True)
    assert _published(hass) == before and engine._charger_saved is None


async def test_the_release_plans_again_with_force(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    engine = coordinator.engine
    await coordinator.async_run_job("charger_test_hold", "charge", 10, 3, 600)
    calls: list[bool] = []
    original = type(engine).charge_if_needed

    def spy(self, force_recalculate=False):
        calls.append(force_recalculate)
        return original(self, force_recalculate)

    with patch.object(type(engine), "charge_if_needed", spy):
        await coordinator.async_run_job("charger_test_release", include_forecast=True)
    assert calls == [True]


async def test_a_held_stop_leaves_the_circuit_open_like_the_plans_stop(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    engine = coordinator.engine
    await coordinator.async_run_job("charger_test_hold", "charge", 10, 3, 600)
    assert engine.current_circuit_amps == [10, 10, 10]
    await coordinator.async_run_job("charger_test_hold", "stop", 0, 0, 600)
    assert engine.current_charging_amps == [0, 0, 0] and engine.current_circuit_amps == [int(a) for a in engine._max_amps()]
    await coordinator.async_run_job("charger_test_release", include_forecast=True)


async def test_the_test_is_stopped_with_the_switch_and_the_plan_comes_back(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    wire_charger(hass, working=False)
    with patch("custom_components.cable_juice_planner.coordinator.charger_test.SAMPLE_SECONDS", 0.01), patch(FOUND, return_value=[]):
        await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
        for _ in range(500):
            await asyncio.sleep(0.01)
            if coordinator.engine._charger_hold is not None:
                break
        assert coordinator.engine._charger_hold is not None and hass.states.get(SWITCH).state == "on"
        await hass.services.async_call("switch", "turn_off", {"entity_id": SWITCH}, blocking=True)
        await _finished(hass)
    state = hass.states.get(RESULT)
    assert state.state == "stopped" and state.attributes["reason"] == "stopped"
    assert coordinator.engine._charger_hold is None and hass.states.get(SWITCH).state == "off"
    assert state.attributes["steps"][-1]["reason"] == "not_reached"


async def test_unloading_during_a_test_puts_the_plan_back_first(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    wire_charger(hass, working=False)
    with patch("custom_components.cable_juice_planner.coordinator.charger_test.SAMPLE_SECONDS", 0.01), patch(FOUND, return_value=[]):
        coordinator.async_run_charger_test()
        for _ in range(500):
            await asyncio.sleep(0.01)
            if coordinator.engine._charger_hold is not None:
                break
        engine = coordinator.engine
        assert engine._charger_hold is not None
        await async_unload_and_join(hass, charger_entry)
    assert engine._charger_hold is None


async def test_a_second_start_while_a_test_runs_does_nothing(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    wire_charger(hass, working=False)
    with patch("custom_components.cable_juice_planner.coordinator.charger_test.SAMPLE_SECONDS", 0.01), patch(FOUND, return_value=[]):
        coordinator.async_run_charger_test()
        task = coordinator._charger_test_task
        coordinator.async_run_charger_test()
        assert coordinator._charger_test_task is task
        coordinator.async_stop_charger_test()
        await _finished(hass)
    assert hass.states.get(RESULT).state == "stopped"


async def test_the_last_result_is_restored_but_a_broken_one_is_not(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    step = {"name": "max", "status": "ok", "reason": None, "phases": 3, "configured_amps": 16, "amps": 16, "watt": 11000, "expected_w": 11040, "attempts": []}
    coordinator.async_restore_charger_test({"status": "running", "steps": [step]})
    assert coordinator.charger_test_state["status"] == "idle"
    coordinator.async_restore_charger_test({"status": "done", "steps": ["garbage"]})
    assert coordinator.charger_test_state["status"] == "idle"
    for broken in (
        {"watt": "lots"}, {"attempts": [{"amps": "x", "watt": 1}]}, {"attempts": "x"}, {"name": "other"}, {"status": "skip", "reason": "made_up"},
        {"voltage": "low"}, {"voltage_setting": [240]},
    ):
        coordinator.async_restore_charger_test({"status": "done", "steps": [{**step, **broken}]})
        assert coordinator.charger_test_state["status"] == "idle", broken
    coordinator.async_restore_charger_test({"status": "done", "steps": [step], "reason": "made_up", "automation": "unknown", "car_phases": 1})
    assert coordinator.charger_test_state["reason"] is None and coordinator.charger_test_state["car_phases"] == 1
    assert coordinator.charger_test_state["automation"] == "unknown"
    coordinator._init_charger_test()
    coordinator.async_restore_charger_test({"status": "done", "steps": [step], "reason": None})
    assert coordinator.charger_test_state["status"] == "done" and "✅" in coordinator.charger_test_state["markdown"]
    measured = {**step, "watt": 10967, "voltage": 229.2, "voltage_setting": 240.0}
    coordinator.async_restore_charger_test({"status": "done", "steps": [measured], "reason": None})
    assert coordinator.charger_test_state["steps"] == [measured] and "229 V" in coordinator.charger_test_state["markdown"]


async def test_the_power_of_a_sensor_that_stopped_reporting_is_not_believed(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    hass.states.async_set(CHARGER_POWER, "3000", {"unit_of_measurement": "W"})
    assert coordinator._charger_power(CHARGER_POWER) == 3000.0
    hass.states.async_set(CHARGER_POWER, "3", {"unit_of_measurement": "kW"})
    assert coordinator._charger_power(CHARGER_POWER) == 3000.0
    hass.states.async_set(CHARGER_POWER, "unavailable")
    assert coordinator._charger_power(CHARGER_POWER) is None
    hass.states.async_set(CHARGER_POWER, "nan")
    assert coordinator._charger_power(CHARGER_POWER) is None
    hass.states.async_set(CHARGER_POWER, "3000")
    with patch("custom_components.cable_juice_planner.coordinator.charger_test.STALE_SECONDS", -1):
        assert coordinator._charger_power(CHARGER_POWER) is None


# --- the charge limit in the car ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "battery", "low", "high", "ten", "expected"),
    [
        (80, 40, 50, 100, False, 90),  # 10 % above
        (95, 40, 50, 100, False, 85),  # no room above: 10 % below, still above the battery
        (95, 85, 50, 100, False, None),  # below would be at the battery level: the car would stop or be done
        (100, 40, 50, 100, False, 90),
        (55, 40, 50, 60, False, None),  # the entity's own maximum is 60 and the minimum 50
        (83, 40, 50, 100, True, 100),  # Kia/Cupra: rounded up to 10 like the plan's limit (93 -> 100)
        (90, 40, 50, 90, True, 80),
        (80, 75, 50, 80, True, None),  # 90 is above the maximum, 70 below the battery level
    ],
)
def test_the_limit_asked_for_is_10_percent_from_the_cars_own(current, battery, low, high, ten, expected) -> None:
    rounding = (lambda value: -(-value // 10) * 10) if ten else (lambda value: value)
    assert limit_test_value(current, battery, low, high, rounding) == expected


@pytest.mark.parametrize(
    ("current", "battery", "published", "own", "expected"),
    [
        (95, None, None, None, None),  # an unknown battery level never allows a lower limit
        (80, None, None, None, 90),  # higher is always fine
        (80, 40, 90, None, 70),  # the plan publishes 90 already: no change for the automation
        (70, 40, None, 80, 60),  # 80 is the car's own from before the test: putting it back would be no change
        (80, 40, 90, 70, None),
    ],
)
def test_the_limit_asked_for_avoids_unknown_levels_and_values_that_send_nothing(current, battery, published, own, expected) -> None:
    assert limit_test_value(current, battery, 50, 100, lambda value: value, published, own) == expected


def _test_limit(hass: HomeAssistant) -> float:
    """The limit the step asks for: 10 % above the car's 80 %, unless the plan publishes that already (then 10 % below)."""
    return 70.0 if hass.states.get(ACTION).attributes.get("charging_limit") == 90 else 90.0


def _car_limit(hass: HomeAssistant) -> float:
    return float(hass.states.get(EV_LIMIT).state)


def _limits_while_held(limits: list, test: float, own: float) -> None:
    """The test limit and then the car's own were published with the action unchanged: the automation sends a changed
    limit only on its own (or with a new charge), never together with a stop."""
    published = [(action, limit) for action, limit in limits if limit is not None]
    test_at = next(index for index, (_, limit) in enumerate(published) if limit == test)
    back_at = next(index for index, (_, limit) in enumerate(published) if index > test_at and limit == own)
    assert published[test_at - 1][0] == published[test_at][0], published
    assert published[back_at - 1][0] == published[back_at][0], published


async def test_the_car_takes_the_limit_and_gets_its_own_back(hass, charger_entry, fast_runner) -> None:
    limits: list = []
    wire_charger(hass, limits=limits)
    test = _test_limit(hass)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    state = hass.states.get(RESULT)
    step = _steps_of(hass)["limit"]
    assert step["status"] == "ok" and (step["limit_from"], step["limit_to"], step["limit_seen"]) == (80, test, test)
    assert _car_limit(hass) == 80
    assert state.attributes["limit_put_back"] == {"ok": True, "limit": 80}
    assert "80 %" in state.attributes["markdown"]
    _limits_while_held(limits, test, 80)
    assert charger_entry.runtime_data.engine._charger_saved is None


async def test_a_car_that_does_not_take_the_limit_fails_the_step(hass, charger_entry, fast_runner) -> None:
    limits: list = []
    wire_charger(hass, limit_follows=lambda value: False, limits=limits)
    test = _test_limit(hass)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    step = _steps_of(hass)["limit"]
    assert step["status"] == "fail" and step["limit_seen"] == 80 and step["limit_to"] == test
    # still the car's own when CJP looked, but the test limit may still reach the car (a slow cloud): not confirmed
    assert hass.states.get(RESULT).attributes["limit_put_back"] == {"ok": False, "limit": 80}
    assert "⚠️" in hass.states.get(RESULT).attributes["markdown"].split("\n\n")[-2]


async def test_a_limit_that_is_not_seen_back_tells_the_user_to_check_the_car(hass, charger_entry, fast_runner) -> None:
    wire_charger(hass, limit_follows=lambda value: value != 80)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    state = hass.states.get(RESULT)
    assert _steps_of(hass)["limit"]["status"] == "ok"
    assert state.attributes["limit_put_back"] == {"ok": False, "limit": 80}
    assert "⚠️" in state.attributes["markdown"].split("\n\n")[-2] and "80 %" in state.attributes["markdown"]


async def test_stopping_during_the_limit_step_puts_the_cars_limit_back_before_the_plan(hass, charger_entry) -> None:
    """The switch turned off while the car is slow to take the limit: the car's own limit is published again with the
    held action, before the release puts the plan's action back."""
    coordinator = charger_entry.runtime_data
    limits: list = []
    wire_charger(hass, limit_follows=lambda value: False, limits=limits)
    test = _test_limit(hass)
    base = "custom_components.cable_juice_planner.coordinator.charger_test"
    with (
        patch(f"{base}.ATTEMPT_TIMEOUT", 0.3), patch(f"{base}.SETTLE_SECONDS", 0), patch(f"{base}.SAMPLE_SECONDS", 0.01),
        patch(f"{base}.OVERDRAW_AFTER_SECONDS", 0), patch(f"{base}.LIMIT_TIMEOUT", 0.5), patch(f"{base}.LIMIT_SAMPLE_SECONDS", 0.01),
        patch(f"{base}.LIMIT_PAUSE_SECONDS", 0), patch(FOUND, return_value=[]),
    ):
        await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
        for _ in range(3000):
            await asyncio.sleep(0.01)
            if (coordinator.engine._charger_hold or {}).get("limit") == test:
                break
        assert (coordinator.engine._charger_hold or {}).get("limit") == test
        await hass.services.async_call("switch", "turn_off", {"entity_id": SWITCH}, blocking=True)
        await _finished(hass)
    state = hass.states.get(RESULT)
    assert state.state == "stopped"
    assert state.attributes["steps"][-1]["reason"] == "not_reached"
    _limits_while_held(limits, test, 80)
    assert coordinator.engine._charger_hold is None and coordinator.engine._charger_saved is None


async def test_stopping_during_the_limit_step_after_an_overdraw_still_tells_the_overdraw(hass, solar_entry) -> None:
    """The charging steps ended on an overdraw, and the switch is turned off during the charge limit step: the reason shown
    is still the overdraw (what the user must fix), not the switch."""
    coordinator = solar_entry.runtime_data
    wire_charger(hass, three_phases_always=True, limit_follows=lambda value: False)
    test = _test_limit(hass)
    base = "custom_components.cable_juice_planner.coordinator.charger_test"
    with (
        patch(f"{base}.ATTEMPT_TIMEOUT", 0.3), patch(f"{base}.SETTLE_SECONDS", 0), patch(f"{base}.SAMPLE_SECONDS", 0.01),
        patch(f"{base}.OVERDRAW_AFTER_SECONDS", 0), patch(f"{base}.LIMIT_TIMEOUT", 0.5), patch(f"{base}.LIMIT_SAMPLE_SECONDS", 0.01),
        patch(f"{base}.LIMIT_PAUSE_SECONDS", 0), patch(FOUND, return_value=[]),
    ):
        await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
        for _ in range(3000):
            await asyncio.sleep(0.01)
            if (coordinator.engine._charger_hold or {}).get("limit") == test:
                break
        assert (coordinator.engine._charger_hold or {}).get("limit") == test
        assert coordinator.engine._charger_hold["kind"] == "stop"  # never the overdrawing charge
        await hass.services.async_call("switch", "turn_off", {"entity_id": SWITCH}, blocking=True)
        await _finished(hass)
    state = hass.states.get(RESULT)
    steps = _steps_of(hass)
    assert state.state == "stopped" and state.attributes["reason"] == "overdraw"
    assert steps["single_max"]["reason"] == "overdraw" and steps["limit"]["reason"] == "not_reached"
    assert coordinator.engine._charger_hold is None


async def test_a_car_that_is_full_during_the_limit_step_still_gets_its_own_limit_back(hass, charger_entry) -> None:
    """The car reports completed while it is at the test's limit: the put-back has no charger status check."""
    coordinator = charger_entry.runtime_data
    limits: list = []
    wire_charger(hass, limit_follows=lambda value: value != 80 or hass.states.get(CHARGER_STATUS).state == "completed", limits=limits)
    test = _test_limit(hass)
    base = "custom_components.cable_juice_planner.coordinator.charger_test"
    with (
        patch(f"{base}.ATTEMPT_TIMEOUT", 0.3), patch(f"{base}.SETTLE_SECONDS", 0), patch(f"{base}.SAMPLE_SECONDS", 0.01),
        patch(f"{base}.OVERDRAW_AFTER_SECONDS", 0), patch(f"{base}.LIMIT_TIMEOUT", 2), patch(f"{base}.LIMIT_SAMPLE_SECONDS", 0.01),
        patch(f"{base}.LIMIT_PAUSE_SECONDS", 0), patch(FOUND, return_value=[]),
    ):
        await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
        for _ in range(3000):
            await asyncio.sleep(0.01)
            if _car_limit(hass) == test:
                break
        assert _car_limit(hass) == test
        hass.states.async_set(CHARGER_STATUS, "completed")
        await _finished(hass)
    assert _car_limit(hass) == 80
    assert hass.states.get(RESULT).attributes["limit_put_back"] == {"ok": True, "limit": 80}
    _limits_while_held(limits, test, 80)


async def test_the_limit_from_before_the_test_is_put_back(hass, charger_entry) -> None:
    """The charge steps may send the plan's limit (70 here): the car gets 80, its own from before the test, back."""
    coordinator = charger_entry.runtime_data
    wire_charger(hass)
    base = "custom_components.cable_juice_planner.coordinator.charger_test"
    with (
        patch(f"{base}.ATTEMPT_TIMEOUT", 0.3), patch(f"{base}.SETTLE_SECONDS", 0), patch(f"{base}.SAMPLE_SECONDS", 0.01),
        patch(f"{base}.OVERDRAW_AFTER_SECONDS", 0), patch(f"{base}.LIMIT_TIMEOUT", 2), patch(f"{base}.LIMIT_SAMPLE_SECONDS", 0.01),
        patch(f"{base}.LIMIT_PAUSE_SECONDS", 0), patch(FOUND, return_value=[]),
    ):
        await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
        for _ in range(3000):
            await asyncio.sleep(0.01)
            if (coordinator.engine._charger_hold or {}).get("kind") == "charge":
                break
        assert (coordinator.engine._charger_hold or {}).get("kind") == "charge"
        hass.states.async_set(EV_LIMIT, "70", hass.states.get(EV_LIMIT).attributes)
        await _finished(hass)
    step = _steps_of(hass)["limit"]
    # from the car's 70: 80 would be its own (no change when put back), so 60 (or 80 + … when 60 is not allowed)
    assert step["status"] == "ok" and step["limit_from"] == 70 and step["limit_to"] not in (70, 80)
    assert _car_limit(hass) == 80
    assert hass.states.get(RESULT).attributes["limit_put_back"] == {"ok": True, "limit": 80}


async def test_a_restart_during_the_limit_step_tells_the_user_to_check_the_car(hass, charger_entry) -> None:
    """HA stopped with the test limit out: the restored result says the car's own limit may not be back."""
    coordinator = charger_entry.runtime_data
    coordinator.async_restore_charger_test({"status": "running", "steps": [], "limit_put_back": {"ok": None, "limit": 80}})
    state = coordinator.charger_test_state
    assert state["status"] == "stopped" and state["reason"] == "stopped"
    assert state["limit_put_back"] == {"ok": False, "limit": 80}
    assert "80 %" in state["markdown"]
    # a test that was running without a test limit out is not restored (as before)
    coordinator._init_charger_test()
    coordinator.async_restore_charger_test({"status": "running", "steps": [], "limit_put_back": None})
    assert coordinator.charger_test_state["status"] == "idle"


async def test_a_restart_during_the_limit_step_is_restored_by_the_sensor(hass) -> None:
    """Through the sensor's own restore: a saved "running" state with the test limit out comes back as stopped."""
    from homeassistant.core import State  # noqa: PLC0415
    from pytest_homeassistant_custom_component.common import mock_restore_cache  # noqa: PLC0415

    mock_restore_cache(hass, [State(RESULT, "running", {"steps": [], "limit_put_back": {"ok": None, "limit": 80}})])
    set_source_states(hass, charger_status="charging")
    entry = make_entry(options=MOCK_OPTIONS)
    await async_setup(hass, entry)
    state = hass.states.get(RESULT)
    assert state.state == "stopped"
    assert state.attributes["limit_put_back"] == {"ok": False, "limit": 80}
    note = state.attributes["markdown"].split("\n\n")[-2]  # the legend (last) has ⚠️ too
    assert "⚠️" in note and "80 %" in note
    await async_unload_and_join(hass, entry)


async def test_an_unknown_battery_level_never_allows_a_lower_test_limit(hass, charger_entry) -> None:
    """The car at 95 %: only 85 % is left, and without a battery level it is not asked for (0 % would allow it)."""
    engine = charger_entry.runtime_data.engine
    hass.states.async_set(EV_LIMIT, "95", hass.states.get(EV_LIMIT).attributes)
    with patch.object(type(engine), "battery_level", lambda self, real=False, unknown=0.0: unknown):
        plan = (await charger_entry.runtime_data.async_run_job("charger_test_limit_plan")).value
    assert plan["current"] == 95 and plan["test"] is None


async def test_unloading_during_the_limit_step_publishes_the_limit_back_without_waiting(hass, charger_entry) -> None:
    """The entry is unloaded while the car has not taken the test limit: the car's own is published back at once (no
    minutes of waiting for the car), and the result says to check it."""
    coordinator = charger_entry.runtime_data
    limits: list = []
    wire_charger(hass, limit_follows=lambda value: False, limits=limits)
    test = _test_limit(hass)
    base = "custom_components.cable_juice_planner.coordinator.charger_test"
    with (
        patch(f"{base}.ATTEMPT_TIMEOUT", 0.3), patch(f"{base}.SETTLE_SECONDS", 0), patch(f"{base}.SAMPLE_SECONDS", 0.01),
        patch(f"{base}.OVERDRAW_AFTER_SECONDS", 0), patch(f"{base}.LIMIT_TIMEOUT", 60), patch(f"{base}.LIMIT_SAMPLE_SECONDS", 0.01),
        patch(f"{base}.LIMIT_PAUSE_SECONDS", 0), patch(FOUND, return_value=[]),
    ):
        await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
        for _ in range(3000):
            await asyncio.sleep(0.01)
            if (coordinator.engine._charger_hold or {}).get("limit") == test:
                break
        assert (coordinator.engine._charger_hold or {}).get("limit") == test
        # the car shows neither the test limit nor its own: a put-back that waited for it would wait the 60 s
        hass.states.async_set(EV_LIMIT, "75", hass.states.get(EV_LIMIT).attributes)
        await asyncio.wait_for(coordinator.async_unload_charger_test(), 10)
    published = [limit for _, limit in limits if limit is not None]
    assert 80 in published[published.index(test):]  # the car's own, published after the test limit
    assert coordinator.charger_test_state["limit_put_back"] == {"ok": False, "limit": 80}


async def test_a_lost_hold_before_the_test_limit_leaves_nothing_to_put_back(hass, charger_entry, fast_runner) -> None:
    """charger_test_limit finds no hold: no test limit was published, so no "could not confirm" either."""
    coordinator = charger_entry.runtime_data
    engine = coordinator.engine
    wire_charger(hass)
    with patch.object(type(engine), "charger_test_limit", lambda self, value, car_limit, seconds: {"status": "no_hold"}), patch(FOUND, return_value=[]):
        await _start(hass)
    step = _steps_of(hass)["limit"]
    assert step["status"] == "skip" and step["reason"] == "not_ready"
    assert hass.states.get(RESULT).attributes["limit_put_back"] is None


async def test_the_pending_put_back_is_not_shown_while_the_test_runs(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    coordinator._publish_charger_test(status="running", steps=[], limit_put_back={"ok": None, "limit": 80})
    assert "80 %" not in coordinator.charger_test_state["markdown"]


async def test_without_steps_for_the_charge_limit_it_is_not_tried(hass, charger_entry, fast_runner) -> None:
    limits: list = []
    wire_charger(hass, limits=limits)
    inputs = [{"start_actions": [{"action": "x"}], "stop_actions": [{"action": "y"}]}]
    with patch(FOUND, return_value=inputs):
        await _start(hass)
    step = _steps_of(hass)["limit"]
    assert step["status"] == "skip" and step["reason"] == "no_steps"
    assert _test_limit(hass) not in {limit for _, limit in limits}
    assert _car_limit(hass) == 80 and hass.states.get(RESULT).attributes["limit_put_back"] is None


async def test_a_car_limit_without_a_number_is_not_tested(hass, charger_entry, fast_runner) -> None:
    limits: list = []
    hass.states.async_set(EV_LIMIT, "unavailable")
    wire_charger(hass, limits=limits)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    step = _steps_of(hass)["limit"]
    assert step["status"] == "skip" and step["reason"] == "limit_no_data"
    assert _test_limit(hass) not in {limit for _, limit in limits} and hass.states.get(RESULT).attributes["limit_put_back"] is None


async def test_a_car_without_a_charge_limit_entity_has_no_limit_step(hass, fast_runner) -> None:
    set_source_states(hass, charger_status="charging")
    entry = make_entry(options={**MOCK_OPTIONS, "charging_limit_entity_id": ""})
    await async_setup(hass, entry)
    wire_charger(hass)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    assert list(_steps_of(hass)) == ["max", "stop"]
    await async_unload_and_join(hass, entry)


async def test_a_hold_that_ran_out_during_the_limit_step_publishes_the_cars_own_limit(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    wire_charger(hass, limit_follows=lambda value: False)
    await coordinator.async_run_job("charger_test_hold", "stop", 0, 0, 600)
    await coordinator.async_run_job("charger_test_limit", 90, 80, 600)
    assert hass.states.get(ACTION).attributes["charging_limit"] == 90
    coordinator.engine._charger_hold["until"] -= datetime.timedelta(hours=1)
    await coordinator.async_run_job("charger_test_published")
    assert coordinator.engine._charger_hold is None
    assert hass.states.get(ACTION).attributes["charging_limit"] == 80


async def test_the_limit_is_only_published_while_a_test_holds_the_charger(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    before = hass.states.get(ACTION).attributes.get("charging_limit")
    assert (await coordinator.async_run_job("charger_test_limit", 90, 80, 600)).value == {"status": "no_hold"}
    assert hass.states.get(ACTION).attributes.get("charging_limit") == before


async def test_a_restored_result_keeps_the_limit_step_and_the_put_back(hass, charger_entry) -> None:
    coordinator = charger_entry.runtime_data
    step = {
        "name": "limit", "status": "ok", "reason": None, "phases": 0, "configured_amps": 0, "amps": 0, "watt": 0, "expected_w": 0,
        "attempts": [], "seconds": 30, "limit_from": 80, "limit_to": 90, "limit_seen": 90,
    }
    coordinator.async_restore_charger_test({"status": "done", "steps": [step], "limit_put_back": {"ok": False, "limit": 80}})
    assert coordinator.charger_test_state["status"] == "done"
    assert coordinator.charger_test_state["limit_put_back"] == {"ok": False, "limit": 80}
    note = coordinator.charger_test_state["markdown"].split("\n\n")[-2]  # the legend (last) has ⚠️ too
    assert "⚠️" in note and "80 %" in note
    coordinator._init_charger_test()
    coordinator.async_restore_charger_test({"status": "done", "steps": [{**step, "limit_to": "lots"}]})
    assert coordinator.charger_test_state["status"] == "idle"
    coordinator.async_restore_charger_test({"status": "done", "steps": [step], "limit_put_back": {"ok": "yes", "limit": 80}})
    assert coordinator.charger_test_state["limit_put_back"] is None
