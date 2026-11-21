"""The test of the user's charger automation: the steps, the held current, the search for the current that works, the runner."""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event

from custom_components.cable_juice_planner.const import CHARGER_TEST_STATUSES, build_config
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
    judge_power,
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

from .conftest import CHARGER_POWER, CHARGER_STATUS, MOCK_OPTIONS, async_setup, async_unload_and_join, make_entry, set_source_states
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
    for extra in ({"limited": True}, {"already_charging": True}, {"limited": True, "already_charging": True}, {"judged_phases": 1})
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
                markdown = build_charger_test_markdown(t, {
                    "status": status, "steps": STEP_RESULTS, "reason": reason, "automation": automation, "amps_control": amps_control,
                    "car_phases": car_phases,
                })
                assert "{" not in markdown and "ui.charger_test" not in markdown
        assert "ui.charger_test" not in build_charger_test_markdown(t, {"status": status, "steps": [], "reason": "completed"})
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


# --- the runner, with a charger that follows the published action -----------------------------------------------


@pytest.fixture
def fast_runner():
    base = "custom_components.cable_juice_planner.coordinator.charger_test"
    with (
        patch(f"{base}.ATTEMPT_TIMEOUT", 0.3), patch(f"{base}.SETTLE_SECONDS", 0), patch(f"{base}.SAMPLE_SECONDS", 0.01),
        patch(f"{base}.OVERDRAW_AFTER_SECONDS", 0),
    ):
        yield


def wire_charger(
    hass: HomeAssistant, max_amp: int = 16, min_amp: int = 6, working: bool = True, car_phases: int = 3, cap_w: float | None = None,
    three_phases_always: bool = False, power_at=None,
):
    """The user's automation and charger: the power follows the published action and amps, like a charger would.

    car_phases: the phases the car's on-board charger uses. cap_w: the car takes no more (a full or cold battery).
    three_phases_always: an automation that does not switch the charger to one phase. power_at(amps, phases): the power
    of a charge instead (a charger that does something else)."""
    seen: list[tuple] = []

    def update(_event=None) -> None:
        state = hass.states.get(ACTION)
        if state is None:
            return
        amps = [int(state.attributes.get(f"phase_{index}_amps") or 0) for index in (1, 2, 3)]
        seen.append((state.state, tuple(amps)))
        power = 0.0
        if state.state == "charge" and working:
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
    assert state.state == "done" and list(steps) == ["max", "stop"]
    assert steps["max"]["status"] == "ok" and steps["max"]["watt"] == 16 * 3 * VOLTAGE
    assert steps["stop"]["status"] == "ok"
    assert ("charge", (16, 16, 16)) in seen
    assert hass.states.get(SWITCH).state == "off"
    assert charger_entry.runtime_data.engine._charger_hold is None


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
    wire_charger(hass, working=False)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    state = hass.states.get(RESULT)
    steps = _steps_of(hass)
    assert state.state == "stopped" and state.attributes["reason"] == "no_charging"
    assert steps["max"]["status"] == "fail" and len(steps["max"]["attempts"]) == 1  # no search on a dead charger
    assert all(steps[name]["status"] == "skip" and steps[name]["reason"] == "not_reached" for name in ("min", "single_max", "single_min", "stop"))
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
    assert list(steps) == ["max", "stop"]
    assert steps["max"]["status"] == "ok" and steps["max"]["expected_w"] == 0  # started: no power to compare with
    assert hass.states.get(RESULT).state == "done"
    assert "Set charging current" in hass.states.get(RESULT).attributes["markdown"]


async def test_a_step_the_automation_has_no_steps_for_is_not_tested(hass, solar_entry, fast_runner) -> None:
    wire_charger(hass)
    found = [{"notify_actions": [{"action": "test.notify"}], "start_actions": [], "stop_actions": []}]
    with patch(FOUND, return_value=found):
        await _start(hass)
    steps = _steps_of(hass)
    assert list(steps) == ["max", "stop"]
    assert all(step["status"] == "skip" and step["reason"] == "no_steps" for step in steps.values())


async def test_a_charger_that_pauses_at_0_amps_is_tested_with_its_current_alone(hass, solar_entry, fast_runner) -> None:
    wire_charger(hass)
    with patch(FOUND, return_value=[{"amps_actions": [{"action": "test.amps"}], "start_actions": [], "stop_actions": []}]):
        await _start(hass)
    steps = _steps_of(hass)
    assert list(steps) == ["max", "min", "single_max", "single_min", "stop"]
    assert all(step["status"] in ("ok", "adjusted") for step in steps.values())


async def test_an_automation_that_does_not_switch_to_one_phase_ends_the_test_at_once(hass, solar_entry, fast_runner) -> None:
    """The load balancing counts on the published amps: a charger taking three phases at "16 A on one phase" is a risk."""
    wire_charger(hass, three_phases_always=True)
    with patch(FOUND, return_value=[]):
        await _start(hass)
    state = hass.states.get(RESULT)
    steps = _steps_of(hass)
    assert state.state == "stopped" and state.attributes["reason"] == "overdraw"
    assert steps["single_max"]["status"] == "fail" and steps["single_max"]["reason"] == "overdraw"
    assert len(steps["single_max"]["attempts"]) == 1  # no search, and never more amps
    assert steps["single_min"]["reason"] == "not_reached" and steps["stop"]["reason"] == "not_reached"
    assert solar_entry.runtime_data.engine._charger_hold is None


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
    assert list(steps) == ["max", "min", "stop"]
    assert all(steps[name]["status"] == "skip" and steps[name]["reason"] == "amps_unproven" for name in ("max", "min"))
    assert steps["stop"]["status"] == "ok"  # the charger does stop: that is proven
    assert "1 af 3" in state.attributes["markdown"] and "4140 W" in state.attributes["markdown"]


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
    assert list(steps) == ["max", "stop"] and state.attributes["car_phases"] == 1
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
    for broken in ({"watt": "lots"}, {"attempts": [{"amps": "x", "watt": 1}]}, {"attempts": "x"}, {"name": "other"}, {"status": "skip", "reason": "made_up"}):
        coordinator.async_restore_charger_test({"status": "done", "steps": [{**step, **broken}]})
        assert coordinator.charger_test_state["status"] == "idle", broken
    coordinator.async_restore_charger_test({"status": "done", "steps": [step], "reason": "made_up", "automation": "unknown", "car_phases": 1})
    assert coordinator.charger_test_state["reason"] is None and coordinator.charger_test_state["car_phases"] == 1
    assert coordinator.charger_test_state["automation"] == "unknown"
    coordinator._init_charger_test()
    coordinator.async_restore_charger_test({"status": "done", "steps": [step], "reason": None})
    assert coordinator.charger_test_state["status"] == "done" and "✅" in coordinator.charger_test_state["markdown"]


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
